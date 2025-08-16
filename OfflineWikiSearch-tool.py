"""
title: OfflineWikiSearch
author: JustinJJA
author_url: https://github.com/justinjja
git_url: https://github.com/justinjja/OfflineWikiSearch/
description: |
  Offline Wikipedia search powered by an OpenSearch/Cirrus index.
  Exposes three tools:
    • wiki.search — BM25 search for candidate pages
    • wiki.get_passages — passage windows from highlights
    • wiki.get_article — exact article slice by page_id or title

  FieldInfo-safe: normalizes Pydantic Field(...) values at runtime so defaults
  are JSON/arithmetic friendly (no FieldInfo objects leaking into logic/outputs).
version: 0.1.0
required_open_webui_version: 0.4.0
requirements: requests, pydantic
licence: MIT
"""

import os
import json
from typing import List, Dict, Optional, Tuple, Any
import requests
from pydantic import BaseModel, Field

# ----------------------------
# Module-level helpers (hidden from LLM)
# ----------------------------


def _safe_json(data: Dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _es_search(
    session: requests.Session,
    es_base: str,
    index: str,
    body: Dict,
    timeout: Tuple[int, int],
) -> Dict:
    url = f"{es_base.rstrip('/')}/{index}/_search"
    r = session.post(url, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _merge_windows(
    windows: List[Tuple[int, int]], overlap: int
) -> List[Tuple[int, int]]:
    if not windows:
        return []
    windows.sort()
    merged = [windows[0]]
    for start, end in windows[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end and (last_end - start) >= overlap:
            merged[-1] = (last_start, max(last_end, end))
        elif start <= last_end and overlap == 0:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _clip_window(center_idx: int, text_len: int, max_chars: int) -> Tuple[int, int]:
    half = max_chars // 2
    start = max(0, center_idx - half)
    end = min(text_len, start + max_chars)
    start = max(0, end - max_chars)
    return start, end


def _resolve(value: Any, fallback: Any, cast: Optional[type] = None) -> Any:
    """
    Normalize possibly-wrapped defaults.

    Purpose
    -------
    Convert Pydantic FieldInfo (or similar) to its default; then apply `fallback`,
    then `cast` (e.g., to int). Prevents "FieldInfo is not JSON serializable"
    and arithmetic/logic involving FieldInfo.

    Parameters
    ----------
    value : Any
        Candidate value (could be a FieldInfo).
    fallback : Any
        Value to use if `value` is None or cast fails.
    cast : Optional[type]
        If provided, attempts to cast the value.

    Returns
    -------
    Any
        A concrete, JSON-serializable primitive ready for use.
    """
    # unwrap FieldInfo if present
    try:
        # pydantic v2 Field(...) -> pydantic.fields.FieldInfo
        from pydantic.fields import FieldInfo  # type: ignore

        if isinstance(value, FieldInfo):
            value = value.default
    except Exception:
        # pydantic v1 or different import — fall through
        pass

    if value is None:
        value = fallback

    # cast strings like "10" to int when needed
    if cast is not None and value is not None:
        try:
            value = cast(value)
        except Exception:
            value = fallback
    return value


# ============================
# Tools (LLM-callable)
# ============================


class Tools:
    """
    Wikipedia (offline) tools for Open WebUI.

    Usage Pattern
    -------------
    1) Call `wiki.search` to get candidate pages for a user query.
    2) Optionally call `wiki.get_passages` to extract relevant passages.
    3) Call `wiki.get_article` to retrieve an exact text window for grounding/citation.

    Notes
    -----
    • All tool methods return JSON **strings** (LLM-friendly).
    • Respect the valves below (OpenSearch URL, index, timeouts, max window).
    """

    class Valves(BaseModel):
        """
        Configurable settings (visible in WebUI).

        You can modify these in the model's Tools settings. Defaults read from
        environment when available.
        """

        opensearch_url: str = Field(
            default=os.getenv("OPENSEARCH_URL", "http://localhost:9200"),
            description="Base URL for OpenSearch (e.g., http://localhost:9200).",
        )
        wiki_index: str = Field(
            default=os.getenv("WIKI_INDEX", "enwiki"),
            description="Index name containing the Wikipedia Cirrus content dump.",
        )
        request_connect_timeout: int = Field(
            default=int(os.getenv("OWUI_CONNECT_TIMEOUT", "5")),
            description="HTTP connect timeout (seconds).",
        )
        request_read_timeout: int = Field(
            default=int(os.getenv("OWUI_READ_TIMEOUT", "30")),
            description="HTTP read timeout (seconds).",
        )
        max_article_window: int = Field(
            default=int(os.getenv("MAX_ARTICLE_WINDOW", "20000")),
            ge=1000,
            le=200000,
            description="Maximum characters wiki.get_article may return per call (hard cap).",
        )

    def __init__(self):
        """Initialize the tool and HTTP session."""
        self.valves = self.Valves()
        self.session = requests.Session()

    # 1) wiki.search
    def wiki_search(
        self,
        query: str = Field(
            ..., description="User query to search Wikipedia with BM25."
        ),
        limit: int = Field(
            5, ge=1, le=20, description="Max number of results to return."
        ),
        namespace: int = Field(0, description="Namespace filter (0 = main/article)."),
    ) -> str:
        """
        wiki.search — Retrieve a ranked list of candidate Wikipedia pages.

        When to use
        -----------
        Use this FIRST to find relevant pages for a user question before fetching
        passages or an article slice.

        Arguments
        ---------
        query : str
            Natural language query (BM25 over Cirrus fields).
        limit : int (1..20), default 5
            Number of hits to return.
        namespace : int, default 0
            Namespace filter (0 = main/article).

        Returns
        -------
        JSON str
            {
              "results": [
                {"page_id": int, "title": str, "opening_text": str, "score": float}
              ],
              "took_ms": int
            }
            On error: {"error": "search_failed: <message>"}

        Notes
        -----
        • Scores come from OpenSearch BM25. Higher is better.
        • Filters to namespace via term query; boosts title/opening_text.
        • For deep grounding, follow with `wiki.get_passages` or `wiki.get_article`.

        Example
        -------
        {"tool": "wiki.search", "args": {"query": "Alan Turing", "limit": 5}}
        """
        # Normalize args (avoid FieldInfo in JSON)
        limit = _resolve(limit, 5, int)
        namespace = _resolve(namespace, 0, int)

        body = {
            "size": limit,
            "_source": ["page_id", "title", "opening_text", "namespace"],
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "type": "best_fields",
                                "fields": [
                                    "title^10",
                                    "opening_text^3",
                                    "text",
                                    "auxiliary_text^2",
                                ],
                            }
                        }
                    ],
                    "filter": {"term": {"namespace": namespace}},
                }
            },
        }
        try:
            resp = _es_search(
                self.session,
                self.valves.opensearch_url,
                self.valves.wiki_index,
                body,
                (self.valves.request_connect_timeout, self.valves.request_read_timeout),
            )
            hits = resp.get("hits", {}).get("hits", [])
            results = []
            for h in hits:
                src = h.get("_source", {})
                results.append(
                    {
                        "page_id": src.get("page_id"),
                        "title": src.get("title"),
                        "opening_text": src.get("opening_text"),
                        "score": h.get("_score"),
                    }
                )
            return _safe_json({"results": results, "took_ms": resp.get("took")})
        except requests.RequestException as e:
            return _safe_json({"error": f"search_failed: {str(e)}"})

    # 2) wiki.get_passages
    def wiki_get_passages(
        self,
        query: str = Field(..., description="Information need (natural language)."),
        page_id: Optional[int] = Field(
            None, description="Scope to a specific article if provided."
        ),
        limit: int = Field(
            6, ge=1, le=20, description="Max number of passages to return."
        ),
        max_chars_per_passage: int = Field(
            1200, ge=200, le=5000, description="Max characters per passage window."
        ),
        overlap: int = Field(
            150,
            ge=0,
            le=500,
            description="Minimum overlap threshold (chars) for merging adjacent windows.",
        ),
    ) -> str:
        """
        wiki.get_passages — Extract relevant passage windows from matched articles.

        When to use
        -----------
        Use after `wiki.search` (or with a known page_id) to get concise, readable
        snippets for grounding/citations. Merges adjacent highlight windows to reduce
        fragmentation.

        Arguments
        ---------
        query : str
            Natural language info need; used to build the highlight query.
        page_id : Optional[int], default None
            If set, restricts to this article; otherwise searches broadly.
        limit : int (1..20), default 6
            Max number of passages to return.
        max_chars_per_passage : int (200..5000), default 1200
            Window size centered on highlight hits or start of text if no highlights.
        overlap : int (0..500), default 150
            Minimum overlap (chars) to merge adjacent windows.

        Returns
        -------
        JSON str
            {
              "passages": [
                {
                  "page_id": int,
                  "title": str,
                  "field": "text" | "auxiliary_text",
                  "passage": str,
                  "char_range": [start, end],
                  "score": float
                }
              ],
              "took_ms": int
            }
            On error: {"error": "get_passages_failed: <message>"}

        Notes
        -----
        • Prefers `auxiliary_text` (lead/summary) before main `text`.
        • Deduplicates identical passage windows across fields.
        • If no highlights are returned, falls back to the leading window.
        • Tune `max_chars_per_passage` for shorter/longer quotes; increase `overlap`
          to encourage merging.

        Example
        -------
        {"tool": "wiki.get_passages", "args": {"query": "Turing Award purpose", "limit": 4}}
        """
        # Normalize args
        pid = _resolve(page_id, None, int) if page_id is not None else None
        limit = _resolve(limit, 6, int)
        max_chars_per_passage = _resolve(max_chars_per_passage, 1200, int)
        overlap = _resolve(overlap, 150, int)

        frag_size = max(120, min(240, max_chars_per_passage // 6))
        num_frags = 30

        passages: List[Dict] = []
        seen_windows_by_doc_field: Dict[Tuple[int, str], List[Tuple[int, int]]] = {}
        seen_passage_texts: set = set()

        def highlight_clause(q: str) -> Dict:
            return {
                "pre_tags": [""],
                "post_tags": [""],
                "fields": {
                    "text": {
                        "type": "unified",
                        "fragment_size": frag_size,
                        "number_of_fragments": num_frags,
                    },
                    "auxiliary_text": {
                        "type": "unified",
                        "fragment_size": frag_size,
                        "number_of_fragments": num_frags,
                    },
                },
                "highlight_query": {
                    "multi_match": {
                        "query": q,
                        "type": "best_fields",
                        "fields": [
                            "title^8",
                            "opening_text^3",
                            "text",
                            "auxiliary_text^2",
                        ],
                    }
                },
            }

        try:
            if pid is None:
                body = {
                    "size": max(limit, 8),
                    "_source": ["page_id", "title", "text", "auxiliary_text"],
                    "query": {
                        "multi_match": {
                            "query": query,
                            "type": "best_fields",
                            "fields": [
                                "title^8",
                                "opening_text^3",
                                "text",
                                "auxiliary_text^2",
                            ],
                        }
                    },
                    "highlight": highlight_clause(query),
                }
            else:
                body = {
                    "size": 1,
                    "_source": ["page_id", "title", "text", "auxiliary_text"],
                    "query": {"bool": {"must": [{"term": {"page_id": pid}}]}},
                    "highlight": highlight_clause(query),
                }

            resp = _es_search(
                self.session,
                self.valves.opensearch_url,
                self.valves.wiki_index,
                body,
                (self.valves.request_connect_timeout, self.valves.request_read_timeout),
            )
            took_ms = resp.get("took")
            hits = resp.get("hits", {}).get("hits", [])

            for h in hits:
                if len(passages) >= limit:
                    break

                src = h.get("_source", {})
                pid_h = src.get("page_id")
                title = src.get("title") or ""
                text = src.get("text") or ""
                aux = src.get("auxiliary_text") or ""
                hl = h.get("highlight", {}) or {}
                text_frags: List[str] = (
                    hl.get("text", []) if isinstance(hl, dict) else []
                )
                aux_frags: List[str] = (
                    hl.get("auxiliary_text", []) if isinstance(hl, dict) else []
                )

                def add_from_field(field_name: str, fulltext: str, frags: List[str]):
                    nonlocal passages
                    used_windows = seen_windows_by_doc_field.get(
                        (pid_h, field_name), []
                    )
                    search_from = 0
                    text_len = len(fulltext)

                    if not frags and fulltext:
                        s, e = _clip_window(0, text_len, max_chars_per_passage)
                        window_text = fulltext[s:e]
                        if window_text and window_text not in seen_passage_texts:
                            passages.append(
                                {
                                    "page_id": pid_h,
                                    "title": title,
                                    "field": field_name,
                                    "passage": window_text,
                                    "char_range": [s, e],
                                    "score": h.get("_score"),
                                }
                            )
                            seen_passage_texts.add(window_text)
                        return

                    for frag in frags:
                        if len(passages) >= limit:
                            break
                        idx = fulltext.find(frag, search_from)
                        if idx == -1:
                            idx = fulltext.find(frag)
                            if idx == -1:
                                continue
                        search_from = idx + len(frag)

                        start, end = _clip_window(idx, text_len, max_chars_per_passage)
                        cand = used_windows + [(start, end)]
                        merged = _merge_windows(cand, overlap)
                        if len(merged) == len(used_windows):
                            continue

                        window_text = fulltext[start:end]
                        if not window_text or window_text in seen_passage_texts:
                            continue

                        passages.append(
                            {
                                "page_id": pid_h,
                                "title": title,
                                "field": field_name,
                                "passage": window_text,
                                "char_range": [start, end],
                                "score": h.get("_score"),
                            }
                        )
                        seen_passage_texts.add(window_text)
                        used_windows = merged

                    seen_windows_by_doc_field[(pid_h, field_name)] = used_windows

                # Prefer auxiliary_text first, then text
                add_from_field("auxiliary_text", aux, aux_frags)
                if len(passages) < limit:
                    add_from_field("text", text, text_frags)

            return _safe_json({"passages": passages[:limit], "took_ms": took_ms})
        except requests.RequestException as e:
            return _safe_json({"error": f"get_passages_failed: {str(e)}"})

    # 3) wiki.get_article
    def wiki_get_article(
        self,
        page_id: Optional[int] = Field(
            None, description="Target article by numeric page_id."
        ),
        title: Optional[str] = Field(
            None, description="Target article by exact title; uses title.keyword."
        ),
        start: int = Field(
            0,
            ge=0,
            description="Start char offset (inclusive) within the main body text.",
        ),
        end: int = Field(
            5000,
            ge=1,
            description="End char offset (exclusive) within the main body text.",
        ),
        include_external_links: bool = Field(
            False,
            description="Set true ONLY if you specifically need outgoing URLs. "
            "This increases payload and is rarely helpful for reasoning.",
        ),
    ) -> str:
        """
        wiki.get_article — Retrieve an exact character slice from an article.

        When to use
        -----------
        Use when you need a precise quote/window from the body text for grounding,
        summarization, or follow-up processing.

        Arguments
        ---------
        page_id : Optional[int], default None
            Selects article by numeric id (preferred for exactness).
        title : Optional[str], default None
            Selects article by exact title (title.keyword). Ignored if page_id is set.
        start : int >= 0, default 0
            Start character offset within `text` (inclusive).
        end : int >= 1, default 5000
            End character offset within `text` (exclusive).
        include_external_links : bool, default False
            Include outgoing links array if available (larger payload).

        Returns
        -------
        JSON str
            {
              "title": str,
              "page_id": int,
              "auxiliary_text": str,
              "start": int,
              "end": int,
              "total_len": int,
              "text_window": str,
              "external_link": [...?]   # only when include_external_links=True
            }
            On error: {"error": "not_found", "selector": {"page_id": <int|None>, "title": <str|None>}}
                      {"error": "get_article_failed: <message>"}

        Notes
        -----
        • Enforces a hard cap from valves: `max_article_window` (end - start).
        • If both `page_id` and `title` are None, returns not_found.
        • For long reads, paginate by increasing (start, end) respecting the cap.

        Example
        -------
        {"tool": "wiki.get_article", "args": {"page_id": 736, "start": 0, "end": 2000}}
        """
        # Normalize args
        pid = _resolve(page_id, None, int) if page_id is not None else None
        start = _resolve(start, 0, int)
        end = _resolve(end, 5000, int)
        include_external_links = bool(_resolve(include_external_links, False, bool))

        # Enforce Valve-based max window
        max_window = int(self.valves.max_article_window)
        if end - start > max_window:
            end = start + max_window

        src_fields = ["page_id", "title", "auxiliary_text", "text"]
        if include_external_links:
            src_fields.append("external_link")

        query = (
            {"term": {"page_id": pid}}
            if pid is not None
            else {"term": {"title.keyword": title}}
        )
        body = {"size": 1, "_source": src_fields, "query": query}

        try:
            resp = _es_search(
                self.session,
                self.valves.opensearch_url,
                self.valves.wiki_index,
                body,
                (self.valves.request_connect_timeout, self.valves.request_read_timeout),
            )
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return _safe_json(
                    {"error": "not_found", "selector": {"page_id": pid, "title": title}}
                )

            src = hits[0].get("_source", {})
            pid_out = src.get("page_id")
            ttl = src.get("title")
            aux = src.get("auxiliary_text") or ""
            text = src.get("text") or ""
            total_len = len(text)

            s = max(0, start)
            e = max(s, min(end, total_len))
            text_window = text[s:e] if s < total_len else ""

            out = {
                "title": ttl,
                "page_id": pid_out,
                "auxiliary_text": aux,
                "start": s,
                "end": e,
                "total_len": total_len,
                "text_window": text_window,
            }
            if include_external_links and "external_link" in src:
                out["external_link"] = src["external_link"]

            return _safe_json(out)
        except requests.RequestException as e:
            return _safe_json({"error": f"get_article_failed: {str(e)}"})
