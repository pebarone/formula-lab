import torch
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
)

MODEL = "Qwen/Qwen3-4B-Instruct-2507"

print("CUDA disponível:", torch.cuda.is_available())
print("GPUs detectadas:", torch.cuda.device_count())

if not torch.cuda.is_available():
    raise RuntimeError("CUDA não está disponível no PyTorch.")

print("GPU:", torch.cuda.get_device_name(0))

torch.cuda.set_device(0)
torch.cuda.empty_cache()

quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
)

print("Carregando tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL)

print("Carregando modelo...")
model = AutoModelForCausalLM.from_pretrained(
    MODEL,
    quantization_config=quant_config,
    device_map={"": "cuda:0"},
    dtype=torch.bfloat16,
    low_cpu_mem_usage=True,
)

messages = [
    {
        "role": "user",
        "content": """
Given this Excel spreadsheet:

A | B | C
Produto | Região | Valor
Mouse | SP | 100
Teclado | RJ | 200
Monitor | SP | 500

Write an Excel formula that sums column C only when column B is "SP".

Return only the Excel formula.
""".strip(),
    }
]

inputs = tokenizer.apply_chat_template(
    messages,
    add_generation_prompt=True,
    tokenize=True,
    return_dict=True,
    return_tensors="pt",
)

inputs = {
    key: value.to("cuda:0")
    for key, value in inputs.items()
}

torch.cuda.reset_peak_memory_stats()

with torch.inference_mode():
    output = model.generate(
        **inputs,
        max_new_tokens=100,
        do_sample=False,
        pad_token_id=tokenizer.eos_token_id,
    )

generated_tokens = output[0][inputs["input_ids"].shape[-1]:]

response = tokenizer.decode(
    generated_tokens,
    skip_special_tokens=True,
)

print("\nResposta:")
print(response.strip())

print("\nVRAM:")
print(
    f"Alocada: "
    f"{torch.cuda.memory_allocated(0) / 1024**3:.2f} GB"
)
print(
    f"Reservada: "
    f"{torch.cuda.memory_reserved(0) / 1024**3:.2f} GB"
)
print(
    f"Pico: "
    f"{torch.cuda.max_memory_allocated(0) / 1024**3:.2f} GB"
)