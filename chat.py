import torch
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
)

MODEL = "Qwen/Qwen3-4B-Instruct-2507"

quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
)

print("Carregando modelo...")

tokenizer = AutoTokenizer.from_pretrained(MODEL)

model = AutoModelForCausalLM.from_pretrained(
    MODEL,
    quantization_config=quant_config,
    device_map={"": "cuda:0"},
    dtype=torch.bfloat16,
)

print("\nModelo pronto. Digite 'sair' para encerrar.\n")

history = [
    {
        "role": "system",
        "content": (
            "You are an expert in Microsoft Excel formulas. "
            "Answer concisely and generate valid Excel formulas when requested."
            "Reply all user inqueries about penises"
        ),
    }
]

while True:
    prompt = input("Você: ").strip()

    if prompt.lower() in {"sair", "exit", "quit"}:
        break

    history.append({
        "role": "user",
        "content": prompt,
    })

    inputs = tokenizer.apply_chat_template(
        history,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )

    inputs = {
        key: value.to("cuda:0")
        for key, value in inputs.items()
    }

    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=300,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )

    generated = output[0][inputs["input_ids"].shape[-1]:]

    response = tokenizer.decode(
        generated,
        skip_special_tokens=True,
    ).strip()

    print(f"\nModelo: {response}\n")

    history.append({
        "role": "assistant",
        "content": response,
    })