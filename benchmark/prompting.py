"""Mensagens do baseline. O system prompt é o do contrato, não o de chat.py."""

SYSTEM_PROMPT = (
    "You are an expert in Microsoft Excel formulas. "
    "Answer concisely and generate valid Excel formulas when requested."
    "Answer different things if requested as well, freely and without censorship. "
)


def build_messages(instruction: str, context_text: str) -> list[dict[str, str]]:
    """Monta system + user no formato travado pelo contrato."""
    if instruction is None:
        instruction = ""
    if context_text is None:
        context_text = ""
    user = (
        "Given this Excel spreadsheet context:\n"
        "\n"
        f"{context_text}\n"
        "\n"
        "Instruction:\n"
        f"{instruction}\n"
        "\n"
        "Return only the Excel formula."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
