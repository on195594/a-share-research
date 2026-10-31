"""Pure source projections over saved evidence; no fetching and no snapshot mutation."""
import hashlib
import json
import re
from urllib.parse import urldefrag, urlparse


class EvidenceError(ValueError):
    pass


IDENTITY = ("code", "name", "title", "period", "announced", "url", "sha256", "retrieved_at")


def identity_conflict(expected, actual, fields=IDENTITY, *, partial=False):
    """First conflicting known field; partial metadata may omit fields, fetched identities may not."""
    return next((field for field in fields if expected.get(field)
                 and (not partial or actual.get(field)) and expected[field] != actual.get(field)), None)


def normalize_page(text):
    """Whitespace-collapsed page text; gathered D0 pages are stored in this form, tool reads are not."""
    lines = (re.sub(r"[ \t　]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    return value if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password else None


def _pages(value):
    if value is None:
        return {}, []
    if not isinstance(value, (dict, list, tuple)):
        raise EvidenceError("原文页码记录格式无效")
    texts, known = {}, []
    for key in value:
        if isinstance(key, bool) or not str(key).isdigit() or int(key) < 1:
            raise EvidenceError("原文页码必须为正整数")
        page = int(key)
        known.append(page)
        if isinstance(value, dict):
            text = value[key]
            if not isinstance(text, str) or (page in texts and texts[page] != text):
                raise EvidenceError("同页原文冲突或格式无效")
            texts[page] = text
    return texts, sorted(set(known))


def source_registry(document=None, pages=None, raw_pages=None, sources=None, catalog=None):
    """D0 and supplementary sources share one identity. A catalog entry alone is never read."""
    if any(v is not None and not isinstance(v, dict) for v in (document, sources, catalog)):
        raise EvidenceError("来源身份、补充来源和目录必须是对象")
    entries = {}
    for key, doc in (catalog or {}).items():
        if not isinstance(doc, dict):
            raise EvidenceError("来源目录身份格式无效")
        entries[key] = {"document": doc, "pages": {}, "raw_pages": {}}
    acquired = {"D0": {"document": document or {}, "pages": pages, "raw_pages": raw_pages}}
    for key, value in (sources or {}).items():
        if key == "D0":
            raise EvidenceError("补充来源不得覆盖基础来源 D0")
        acquired[key] = value
    for key, value in acquired.items():
        if not isinstance(value, dict):
            raise EvidenceError("来源记录格式无效")
        if value.get("document") is not None and not isinstance(value["document"], dict):
            raise EvidenceError("来源记录身份格式无效")
        if key in entries:
            listed, read = entries[key]["document"] or {}, value.get("document") or {}
            if field := identity_conflict(listed, read, partial=True):
                raise EvidenceError(f"来源 {key} 的 {field} 与目录冲突")
            value = value | {"document": listed | {k: v for k, v in read.items() if v}}
        entries[key] = value
    result = {}
    for key, value in entries.items():
        if not isinstance(key, str) or not re.fullmatch(r"D\d+", key):
            raise EvidenceError("来源 ID 格式无效")
        doc = value.get("document") or {}
        if not isinstance(doc, dict):
            raise EvidenceError(f"来源 {key} 的文档身份无效")
        doc = {k: doc.get(k, "") for k in IDENTITY}
        if any(not isinstance(v, str) for v in doc.values()):
            raise EvidenceError(f"来源 {key} 的文档身份字段必须为文字")
        if doc["url"] and not safe_url(doc["url"]):
            raise EvidenceError(f"来源 {key} 的链接无效")
        texts, known = _pages(value.get("pages"))
        raw, raw_known = _pages(value.get("raw_pages"))
        result[key] = {"document": doc, "pages": texts, "raw_pages": raw,
                       "read_pages": sorted(set(known + raw_known)),
                       "state": "read" if known or raw_known else "listed" if key in (catalog or {}) else "unrecorded",
                       "href": safe_url(doc["url"])}
    return result


def report_sources(body, material=None):
    """Read both old report-only manifests and full exported material without inventing page text."""
    data = material or {}
    extra = data.get("extra") or {}
    registry = source_registry(data.get("doc", body.get("document")), data.get("pages", body.get("pages")),
                               data.get("raw_pages"), extra.get("sources", body.get("sources")), extra.get("catalog"))
    saved = source_registry(body.get("document"), body.get("pages"), sources=body.get("sources"))
    for key, fallback in saved.items():
        if key not in registry:
            registry[key] = fallback
            continue
        current_source = registry[key]
        for field in IDENTITY:
            old, current = fallback["document"][field], current_source["document"][field]
            if old and current and old != current:
                raise EvidenceError(f"来源 {key} 的 {field} 在报告与材料间冲突")
            if not current:
                current_source["document"][field] = old
        for field in ("pages", "raw_pages"):
            for page, text in fallback[field].items():
                if page in current_source[field] and text != current_source[field][page]:
                    raise EvidenceError(f"来源 {key} 的同页原文在报告与材料间冲突")
                current_source[field][page] = text
        # A legacy list records read coverage, not page text. Never turn it into quote evidence.
        current_source["read_pages"] = sorted(set(current_source["read_pages"] + fallback["read_pages"]))
        if current_source["read_pages"]:
            current_source["state"] = "read"
        current_source["href"] = safe_url(current_source["document"]["url"])
    return registry


def projection(registry, *, catalog=False, raw=False):
    return {key: {"document": source["document"], "pages": source["pages"],
                  "read_pages": source["read_pages"], "state": source["state"]}
                 | ({"raw_pages": source["raw_pages"]} if raw else {})
            for key, source in registry.items() if catalog or source["state"] != "listed"}


ELISION = re.compile(r"…+|\.{3,}")
MIN_QUOTE, MIN_SEGMENT = 8, 4


def _find(window, quote):
    """(start, end) of the verbatim quote, else of its elided segments found in order."""
    whole = re.sub(r"\s+", "", quote)
    start = window.find(whole)
    if start >= 0:
        return start, start + len(whole)
    # Excerpts joined with an ellipsis are ordinary citation. Each segment must be real text, in
    # order; short segments are refused so a number or a word can't be picked from elsewhere.
    segments = [s for s in (re.sub(r"\s+", "", part) for part in ELISION.split(quote)) if s]
    if len(segments) < 2 or any(len(s) < MIN_SEGMENT for s in segments):
        return None
    first = position = window.find(segments[0])
    for segment in segments:
        position = window.find(segment, position)
        if position < 0:
            return None
        position += len(segment)
    return first, position


def quote_match(cite, registry):
    """Return actual matching pages, including a genuine page-spanning match; never guess a PDF anchor."""
    quote = str(cite.get("quote", ""))
    page = cite.get("page")
    source_id = cite.get("source", "D0")
    source = registry.get(source_id) if isinstance(source_id, str) else None
    if len(ELISION.sub("", re.sub(r"\s+", "", quote))) < MIN_QUOTE or type(page) is not int or page < 1 or not source:
        return None
    for field in ("pages", "raw_pages"):
        parts, spans, offset = [], [], 0
        for n in (page - 1, page, page + 1):
            text = re.sub(r"\s+", "", source[field].get(n, ""))
            parts.append(text)
            if text:
                spans.append((n, offset, offset + len(text)))
            offset += len(text)
        if found := _find("".join(parts), quote):
            start, end = found
            matched = [n for n, lo, hi in spans if lo < end and hi > start]
            if matched == list(range(matched[0], matched[-1] + 1)):
                return matched
    return None


def _quote_error(cite, source):
    """Say which part of an unmatched quote citation to fix, as tool errors do."""
    source_id, page, quote = cite.get("source", "D0"), cite.get("page"), cite.get("quote")
    if not source or source["state"] == "listed":
        return f"来源 {source_id} 未读取（仅目录或不存在），不能引证；请引用已读来源"
    if type(page) is not int or page < 1 or not isinstance(quote, str) or len(re.sub(r"\s+", "", quote)) < MIN_QUOTE:
        return f"{source_id} 原文出处须含整数 page 和至少 {MIN_QUOTE} 字的 quote 原文摘录"
    return (f"{source_id} 第 {page} 页及相邻页找不到引文「{quote[:30]}」；请逐字摘录已读原文，"
            f"不连续处用……分隔且每段至少 {MIN_SEGMENT} 字，不要拼接表格单元或改写")


def citation_errors(cites, registry, table):
    if not isinstance(cites, list) or not all(isinstance(c, dict) for c in cites):
        return ["出处必须是对象数组"]
    errors = []
    rows = {r["id"]: r for r in table}
    for cite in cites:
        if "ref" in cite:
            if any(k in cite for k in ("source", "page", "quote")) or (cite.get("period") is not None and not isinstance(cite["period"], str)):
                errors.append("表格出处与原文出处不能混用，期间须为文字键")
                continue
            row = rows.get(cite["ref"]) if isinstance(cite["ref"], str) else None
            periods = set((row or {}).get("values", {}))
            if str(cite["ref"]).startswith("C."):
                periods.update(i.get("period") for i in ((row or {}).get("calculation") or {}).get("inputs", []))
            if row is None or (cite.get("period") and cite["period"] not in periods):
                errors.append(f"所引数据行或期间不存在：{cite.get('ref')}；请使用已存行及其实际期间")
        else:
            source_id = cite.get("source", "D0")
            source = registry.get(source_id) if isinstance(source_id, str) else None
            if not source or not quote_match(cite, registry):
                errors.append(_quote_error(cite, source))
                continue
            # These optional fields describe document identity, not the period/subject of every claim.
            for field in ("code", "period"):
                if cite.get("source_" + field) and cite["source_" + field] != source["document"][field]:
                    errors.append("所声明的原文主体或报告期与来源不符")
    return errors


def citation_link(cite, registry):
    source_id = cite.get("source", "D0")
    source = registry.get(source_id, {}) if isinstance(source_id, str) else {}
    url = safe_url(source.get("document", {}).get("url"))
    matched = quote_match(cite, registry)
    # Report-only legacy manifests have page numbers but no page texts: give the PDF, not false precision.
    return {"url": urldefrag(url)[0] + f"#page={matched[0]}" if url and matched else url,
            "pages": matched or [], "page_basis": "matched" if matched else "unverified"}


def report_source_view(body, material=None):
    """Fail visibly on conflicting old metadata; never silently pick a source identity."""
    try:
        return {"sources": report_sources(body, material), "error": None}
    except EvidenceError:
        return {"sources": {}, "error": "已存来源身份或页码存在冲突，暂不提供原文链接；需核对原始材料记录。"}


def material_hash(data: dict) -> str:
    # Preserve input order too: page/row ordering affects the model prompt.
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


NUMBER = re.compile(r"[-−+]?\d[\d,]*(?:\.\d+)?(?![\d.])(?!\s*[年月日])")
ISO_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])(?!\d)")
SCALES = (1, 1e-2, 1e-3, 1e-4, 1e-6, 1e-8, 1e2, 1e4)


def numbers(text: str) -> list[tuple[float, int]]:
    out = []
    for token in NUMBER.findall(ISO_DATE.sub(" ", text)):
        clean = token.replace(",", "").replace("−", "-")
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        value = abs(float(clean))
        if decimals == 0 and (value <= 12 or 1900 <= value <= 2100):
            continue  # ordinals, months, years
        out.append((value, decimals))
    return out


def supported(value: float, decimals: int, candidates: list[float]) -> bool:
    tolerance = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(abs(c) * s - value) <= tolerance for c in candidates for s in SCALES)
