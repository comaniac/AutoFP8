import argparse
import re
from typing import Tuple

import torch
import torch.functional as F
import transformers
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer


# HACK: override the dtype_byte_size function in transformers to support float8 types
def new_dtype_byte_size(dtype):
    if dtype == torch.bool:
        return 1 / 8
    bit_search = re.search(r"[^\d](\d+)_?", str(dtype))
    if bit_search is None:
        raise ValueError(f"`dtype` is not a valid dtype: {dtype}.")
    bit_size = int(bit_search.groups()[0])
    return bit_size // 8


transformers.modeling_utils.dtype_byte_size = new_dtype_byte_size


def per_tensor_quantize(tensor: torch.Tensor) -> Tuple[torch.Tensor, float]:
    """Quantize a tensor using per-tensor static scaling factor.

    Args:
        tensor: The input tensor.
    """
    finfo = torch.finfo(torch.float8_e4m3fn)
    # Calculate the scale as dtype max divided by absmax.
    # Since .abs() creates a new tensor, we use aminmax to get
    # the min and max first and then calculate the absmax.
    min_val, max_val = tensor.aminmax()
    amax = min_val.abs().max(max_val.abs())
    scale = finfo.max / amax.clamp(min=1e-12)
    # scale and clamp the tensor to bring it to
    # the representative range of float8 data type
    # (as default cast is unsaturated)
    qweight = (tensor * scale).clamp(min=finfo.min, max=finfo.max)
    # Return both float8 data and the inverse scale (as float),
    # as both required as inputs to torch._scaled_mm
    qweight = qweight.to(torch.float8_e4m3fn)
    scale = scale.float().reciprocal()
    return qweight, scale


def fp8_gemm(A, A_scale, B, B_scale, bias, out_dtype):
    cuda_compute_capability = torch.cuda.get_device_capability()
    if cuda_compute_capability >= (9, 0):
        output, _ = torch._scaled_mm(
            A,
            B.t(),
            out_dtype=out_dtype,
            scale_a=A_scale,
            scale_b=B_scale,
            bias=bias,
        )
    else:
        output = torch.nn.functional.linear(
            A.to(out_dtype) * A_scale,
            B.to(out_dtype) * B_scale.to(out_dtype),
            bias=bias,
        )
    return output


class FP8StaticLinearQuantizer(torch.nn.Module):
    def __init__(self, qweight, weight_scale):
        super().__init__()
        self.weight = torch.nn.Parameter(qweight, requires_grad=False)
        self.weight_scale = torch.nn.Parameter(weight_scale, requires_grad=False)
        self.act_scale = None

    def forward(self, x):
        # Dynamically quantize
        qinput, x_act_scale = per_tensor_quantize(x)

        # Update scale if needed.
        if self.act_scale is None:
            self.act_scale = torch.nn.Parameter(x_act_scale)
        elif x_act_scale > self.act_scale:
            self.act_scale = torch.nn.Parameter(x_act_scale)

        # Pass quantized to next layer so it has realistic data.
        output = fp8_gemm(
            A=qinput,
            A_scale=self.act_scale,
            B=self.weight,
            B_scale=self.weight_scale,
            bias=None,
            out_dtype=x.dtype,
        )
        return output


class FP8StaticLinear(torch.nn.Module):
    def __init__(self, qweight, weight_scale, act_scale=0.0):
        super().__init__()
        self.weight = torch.nn.Parameter(qweight, requires_grad=False)
        self.weight_scale = torch.nn.Parameter(weight_scale, requires_grad=False)
        self.act_scale = torch.nn.Parameter(act_scale, requires_grad=False)

    def per_tensor_quantize(
        self, tensor: torch.Tensor, inv_scale: float
    ) -> torch.Tensor:
        # Scale and clamp the tensor to bring it to
        # the representative range of float8 data type
        # (as default cast is unsaturated)
        finfo = torch.finfo(torch.float8_e4m3fn)
        qweight = (tensor / inv_scale).clamp(min=finfo.min, max=finfo.max)
        return qweight.to(torch.float8_e4m3fn)

    def forward(self, x):
        qinput = self.per_tensor_quantize(x, inv_scale=self.act_scale)
        output = fp8_gemm(
            A=qinput,
            A_scale=self.act_scale,
            B=self.weight,
            B_scale=self.weight_scale,
            bias=None,
            out_dtype=x.dtype,
        )
        return output


class FP8DynamicLinear(torch.nn.Module):
    def __init__(self, qweight, scale):
        super().__init__()
        self.weight = torch.nn.Parameter(qweight, requires_grad=False)
        self.weight_scale = torch.nn.Parameter(scale, requires_grad=False)

    def forward(self, x):
        qinput, x_scale = per_tensor_quantize(x)
        output = fp8_gemm(
            A=qinput,
            A_scale=x_scale,
            B=self.weight,
            B_scale=self.weight_scale,
            bias=None,
            out_dtype=x.dtype,
        )
        return output


def quantize_weights(model):
    for name, linear in model.model.named_modules():
        if not isinstance(linear, torch.nn.Linear):
            continue
        quant_weight, quant_scale = per_tensor_quantize(linear.weight)
        quant_linear = FP8DynamicLinear(quant_weight, quant_scale)
        if "." in name:
            parent_name = name.rsplit(".", 1)[0]
            child_name = name[len(parent_name) + 1 :]
            parent = model.model.get_submodule(parent_name)
        else:
            parent_name = ""
            parent = model.model
            child_name = name
        setattr(parent, child_name, quant_linear)


def quantize_activations(model, calibration_tokens):
    # Replace layers with quantizer.
    for name, dynamic_quant_linear in model.model.named_modules():
        if not isinstance(dynamic_quant_linear, FP8DynamicLinear):
            continue
        quantizer = FP8StaticLinearQuantizer(
            dynamic_quant_linear.weight, dynamic_quant_linear.weight_scale
        )
        if "." in name:
            parent_name = name.rsplit(".", 1)[0]
            child_name = name[len(parent_name) + 1 :]
            parent = model.model.get_submodule(parent_name)
        else:
            parent_name = ""
            parent = model.model
            child_name = name
        setattr(parent, child_name, quantizer)

    # Calibration.
    for row_idx in range(calibration_tokens.shape[0]):
        _ = model(calibration_tokens[row_idx].reshape(1, -1))

    # Replace quantizer with StaticLayer.
    for name, quantizer in model.model.named_modules():
        if not isinstance(quantizer, FP8StaticLinearQuantizer):
            continue
        static_proj = FP8StaticLinear(
            quantizer.weight, quantizer.weight_scale, quantizer.act_scale
        )
        if "." in name:
            parent_name = name.rsplit(".", 1)[0]
            child_name = name[len(parent_name) + 1 :]
            parent = model.model.get_submodule(parent_name)
        else:
            parent_name = ""
            parent = model.model
            child_name = name
        setattr(parent, child_name, static_proj)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", type=str)
    parser.add_argument("--save-dir", type=str)
    # parser.add_argument("--static-act", action="store_true")
    parser.add_argument("--num-samples", type=int, default=512)
    parser.add_argument("--max-seq-len", type=int, default=512)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    sample_input_tokens = tokenizer.apply_chat_template(
        [{"role": "user", "content": "What is your name?"}],
        add_generation_prompt=True,
        return_tensors="pt",
    ).to("cuda")

    ds = load_dataset("HuggingFaceH4/ultrachat_200k", split="train_sft")
    ds = ds.shuffle(seed=42).select(range(args.num_samples))
    ds = ds.map(
        lambda batch: {
            "text": tokenizer.apply_chat_template(batch["messages"], tokenize=False)
        }
    )
    tokenizer.pad_token_id = tokenizer.eos_token_id
    calibration_tokens = tokenizer(
        ds["text"],
        return_tensors="pt",
        truncation=True,
        padding="max_length",
        max_length=args.max_seq_len,
        add_special_tokens=False,
    ).input_ids.to("cuda")
    print("Calibration tokens:", calibration_tokens.shape)

    # Load and test the model
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id, torch_dtype="auto", device_map="auto"
    )
    output = model.generate(input_ids=sample_input_tokens, max_new_tokens=20)
    print("ORIGINAL:\n", tokenizer.decode(output[0]), "\n\n")

    # Quantize weights.
    quantize_weights(model)
    output = model.generate(input_ids=sample_input_tokens, max_new_tokens=20)
    print("WEIGHT QUANT:\n", tokenizer.decode(output[0]), "\n\n")

    # Quantize activations.
    quantize_activations(model, calibration_tokens=calibration_tokens)
    output = model.generate(input_ids=sample_input_tokens, max_new_tokens=20)
    print("ACT QUANT:\n", tokenizer.decode(output[0]), "\n\n")

    # Save the model fully quantized
    print(f"Saving the model to {args.save_dir}")
    static_q_dict = {"quantization_config": {"quant_method": "fp8", "scheme": "static"}}
    model.config.update(static_q_dict)
    model.save_pretrained(args.save_dir)
    tokenizer.save_pretrained(args.save_dir)