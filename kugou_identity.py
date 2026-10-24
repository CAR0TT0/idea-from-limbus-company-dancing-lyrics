"""Conservative title equivalence for KuGou's translated display aliases."""
import re
import unicodedata


def normalized(value):
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", str(value)).casefold())


def title_keys(value):
    text = unicodedata.normalize("NFKC", str(value)).strip()
    keys = {normalized(text)}
    match = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", text)
    if match:
        base, alias = match.groups()
        # The client adds Chinese translations to foreign titles. Version and
        # performance qualifiers remain part of the identity, including live,
        # remixes and Chinese version labels; do not use general fuzzy matching.
        foreign = re.search(r"[A-Za-z\u3040-\u30ff\uac00-\ud7af]", base)
        chinese = re.search(r"[\u3400-\u9fff]", alias)
        qualifier = re.search(r"版|现场|伴奏|翻唱|混音|原唱|录音|合唱|铃声|剪辑|串烧|live|remix|cover|instrumental|acoustic", alias, re.I)
        if foreign and chinese and not qualifier and not re.search(r"[\u3040-\u30ff\uac00-\ud7af]", alias):
            keys.add(normalized(base))
    return keys - {""}


def titles_match(first, second):
    return bool(title_keys(first) & title_keys(second))


def artist_keys(value):
    """Allow an explicit cross-script name alias, never drop collaborators."""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    keys = {normalized(text)}
    match = re.fullmatch(r"([^()]+?)\s*\(([^()]+)\)", text)
    if match:
        base, alias = match.groups()
        qualifier = re.search(r"\b(?:feat|ft|featuring|with|vs|live|remix|cover|instrumental|acoustic|piano)\b|版|现场|伴奏|翻唱|混音|合唱|合作|原唱|录音", alias, re.I)
        def scripts(name):
            return {label for label, pattern in (
                ("latin", r"[A-Za-z]"), ("han", r"[\u3400-\u9fff]"),
                ("kana", r"[\u3040-\u30ff]"), ("hangul", r"[\uac00-\ud7af]")) if re.search(pattern, name)}
        original, translated = scripts(base), scripts(alias)
        if (not qualifier and original and translated and original.isdisjoint(translated)
                and not re.search(r"[&+/、,;\d]", alias)):
            keys.add(normalized(base))
    return keys - {""}


def artists_match(first, second):
    return bool(artist_keys(first) & artist_keys(second))
