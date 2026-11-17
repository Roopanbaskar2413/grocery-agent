"""Rule-based product matching. No LLM. Order of attempts:
1) the product the household chose before (preferred map)
2) fuzzy name match on the search results, with a word blocklist
Out of stock and no match are different outcomes."""
import difflib
from dataclasses import dataclass
from typing import Optional

from .models import Product

BLOCKLIST = {"sauce", "ketchup", "puree", "seeds", "chips", "powder", "paste", "masala"}
MIN_SCORE = 0.6


def normalize(name: str) -> str:
    n = name.lower().strip()
    return n[:-1] if n.endswith("s") and len(n) > 3 else n


@dataclass
class MatchResult:
    kind: str                    # "match", "out_of_stock", "no_match"
    product: Optional[Product] = None


def _score(item: str, product: Product) -> float:
    item_n = normalize(item)
    words = [normalize(w) for w in product.name.split()]
    best = max(difflib.SequenceMatcher(None, item_n, w).ratio() for w in words)
    full = difflib.SequenceMatcher(None, item_n, normalize(product.name)).ratio()
    return max(best, full)


def match_item(item_name: str, candidates, preferred: Optional[dict] = None) -> MatchResult:
    item_words = set(item_name.lower().split())
    pool = []
    for p in candidates:
        pwords = set(p.name.lower().split())
        # a blocked word only counts as a mismatch if the list item did not ask for it
        if (pwords & BLOCKLIST) - item_words:
            continue
        pool.append((p, _score(item_name, p)))
    pool = [(p, s) for p, s in pool if s >= MIN_SCORE]
    if not pool:
        return MatchResult("no_match")

    pref_id = (preferred or {}).get(item_name.lower())
    if pref_id:
        for p, _ in pool:
            if p.id == pref_id:
                return MatchResult("match", p) if p.in_stock else MatchResult("out_of_stock", p)

    pool.sort(key=lambda ps: (-ps[1], ps[0].price))
    in_stock = [(p, s) for p, s in pool if p.in_stock]
    if not in_stock:
        return MatchResult("out_of_stock", pool[0][0])
    return MatchResult("match", in_stock[0][0])
