"""Igualdade de fórmulas de Excel depois da normalização do contrato.

Não há equivalência semântica: outra fórmula que calcule o mesmo valor erra.
Maiúsculas ASCII dentro de aspas duplas são igualadas, inclusive onde o Excel ainda distinguiria EXACT, FIND, FINDB, SUBSTITUTE ou um texto devolvido, como ="Hello" e ="hello".
"""

def _first_equals_line(text: str) -> str | None:
    lines = text.splitlines()
    if not lines:
        lines = [text]
    for line in lines:
        candidate = line.strip()
        if len(candidate) >= 2 and candidate.startswith("`") and candidate.endswith("`"):
            candidate = candidate.strip("`").strip()
        if candidate.startswith("="):
            return candidate
    return None


def _fenced_blocks(text: str) -> list[str]:
    marker = "```"
    blocks: list[str] = []
    search_from = 0
    while True:
        start = text.find(marker, search_from)
        if start < 0:
            break
        content_start = start + len(marker)
        end = text.find(marker, content_start)
        if end < 0:
            break
        block = text[content_start:end]
        newline = block.find("\n")
        if newline >= 0:
            block = block[newline + 1 :]
        blocks.append(block)
        search_from = end + len(marker)
    return blocks


def extract_prediction(response: str) -> str:
    """Primeira fórmula da resposta, com prioridade para bloco cercado por crases."""
    if response is None:
        return ""
    text = str(response)
    for block in _fenced_blocks(text):
        found = _first_equals_line(block)
        if found is not None:
            return found
    found = _first_equals_line(text)
    if found is not None:
        return found
    return text.strip()


def _uppercase_ascii(char: str) -> str:
    code = ord(char)
    if 97 <= code <= 122:
        return chr(code - 32)
    return char


def normalize_formula(formula: str) -> str:
    """Remove branco fora de literais e põe em maiúsculas só letras ASCII a–z.

    Vale fora de literais e dentro de aspas duplas. Aspas simples preservam o texto.
    """
    if formula is None:
        return ""
    text = str(formula)
    out: list[str] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char in {'"', "'"}:
            quote = char
            fold = quote == '"'
            out.append(char)
            index += 1
            while index < size:
                current = text[index]
                out.append(_uppercase_ascii(current) if fold else current)
                index += 1
                if current != quote:
                    continue
                # Aspas duplicadas são escape do Excel e continuam no literal.
                if index < size and text[index] == quote:
                    out.append(text[index])
                    index += 1
                    continue
                break
            continue
        if char.isspace():
            index += 1
            continue
        out.append(_uppercase_ascii(char))
        index += 1
    return "".join(out)


def formulas_match(expected: str, predicted: str) -> bool:
    """Compara gabarito e resposta. String vazia nunca é acerto."""
    normalized_expected = normalize_formula("" if expected is None else str(expected))
    normalized_predicted = normalize_formula(extract_prediction(predicted))
    if normalized_expected == "" or normalized_predicted == "":
        return False
    return normalized_expected == normalized_predicted
