"""Civic complaint taxonomy, keyword baseline and synthetic complaint generator.

The generator produces clearly SYNTHETIC complaint text from templates. It contains no real citizen data.
"""

from __future__ import annotations

import random

CATEGORIES = (
    "roads", "streetlight", "garbage", "water_supply", "drainage", "water_logging",
    "sewerage", "trees", "stray_animals", "encroachment", "noise", "construction",
)

KEYWORDS: dict[str, tuple[str, ...]] = {
    "roads": ("pothole", "road", "crater", "asphalt", "speed breaker", "footpath", "pavement", "divider"),
    "streetlight": ("streetlight", "street light", "lamp", "light pole", "dark", "bulb"),
    "garbage": ("garbage", "waste", "trash", "dump", "litter", "bin", "kachra", "rubbish"),
    "water_supply": ("no water", "water supply", "tap", "pipeline", "low pressure", "contaminated water", "pani", "tanker"),
    "drainage": ("drain", "nala", "clogged", "blocked drain", "gutter", "storm water"),
    "water_logging": ("waterlogging", "water logging", "flooded", "inundated", "standing water", "knee deep"),
    "sewerage": ("sewage", "sewer", "manhole", "overflowing", "foul smell", "septic"),
    "trees": ("tree", "branch", "fallen tree", "pruning", "uprooted"),
    "stray_animals": ("stray", "dog", "cattle", "cow", "monkey", "bite"),
    "encroachment": ("encroach", "illegal shop", "hawker", "occupied footpath", "unauthorised structure"),
    "noise": ("noise", "loudspeaker", "loud music", "honking", "dj"),
    "construction": ("construction", "debris", "building material", "excavation", "digging", "rubble"),
}

_TEMPLATES: dict[str, tuple[str, ...]] = {
    "roads": ("Deep potholes all along {street}, vehicles getting damaged", "The road near {landmark} has caved in", "Broken asphalt and loose gravel on {street}",
              "Huge pothole on {street} near {landmark}, two-wheelers are falling", "Road surface completely broken at {street}",
              "Footpath tiles missing outside {landmark}", "Speed breaker unmarked on {street}, accidents happening",
              "Road divider damaged near {landmark}"),
    "streetlight": ("No street lighting near {landmark}, unsafe for women at night", "Bulb fused in the lamp post at {street}", "Dark stretch on {street}, lights broken",
              "Streetlight not working on {street} for {n} days", "Entire lane dark near {landmark}, lamps not on",
                    "Light pole leaning dangerously at {street}", "Street lights stay on during the day at {street}"),
    "garbage": ("Waste not picked up from {street}, bins full", "Rubbish piling up behind {landmark}", "Litter and trash everywhere on {street}",
              "Garbage not collected in {street} for {n} days", "Overflowing waste bin near {landmark}",
                "People dumping trash on vacant plot at {street}", "Kachra lying near {landmark}, very bad smell"),
    "water_supply": ("Tap water supply stopped at {street}", "Pani nahi aa raha hai {street} mein since {n} days", "Need water tanker, no supply near {landmark}",
              "No water supply in {street} since {n} days", "Very low pressure in taps at {street}",
                     "Contaminated water coming from tap near {landmark}", "Pipeline burst near {landmark}, water wasted"),
    "drainage": ("Gutter is clogged on {street}", "Nala near {landmark} needs desilting, blocked with garbage", "Blocked drain causing bad smell at {street}",
              "Drain blocked near {landmark}, water not flowing", "Open nala overflowing on {street}",
                 "Storm water drain clogged with plastic at {street}"),
    "water_logging": ("Road flooded near {landmark} after heavy rain", "Water inundated {street}, cannot walk", "After every rain {street} is under water",
              "Waterlogging on {street} after rain, knee deep water", "Underpass near {landmark} flooded again",
                      "Standing water for {n} days at {street}, mosquitoes breeding"),
    "sewerage": ("Sewer line choked at {street}, dirty water on road", "Septic overflow near {landmark}", "Open manhole on {street}, very dangerous",
              "Sewage overflowing from manhole at {street}", "Manhole cover missing near {landmark}",
                 "Foul smell from sewer line on {street}"),
    "trees": ("Uprooted tree lying on {street}", "Big branch broke and fell near {landmark}", "Tree pruning needed at {street}, branches on wires",
              "Fallen tree blocking {street}", "Tree branch about to fall on wires near {landmark}",
              "Tree needs pruning, touching power lines at {street}"),
    "stray_animals": ("Too many stray dogs near {landmark}", "Cows sitting in the middle of {street}", "Monkey menace at {street}, people scared",
              "Stray dogs chasing children on {street}", "Cattle roaming on main road near {landmark}",
                      "Dog bite incident at {street}, please act"),
    "encroachment": ("Shops have encroached the footpath at {street}", "Footpath occupied by vendors near {landmark}", "Illegal extension of building onto road at {street}",
              "Hawkers have occupied the footpath at {street}", "Illegal shop extension blocking road near {landmark}",
                     "Unauthorised structure built on public land at {street}"),
    "noise": ("Very loud music every night near {landmark}", "Loudspeaker from function hall at {street} past midnight", "Unbearable honking noise at {street} junction",
              "Loudspeaker noise late night near {landmark}", "Loud music from {landmark} after 10 pm",
              "Constant honking and DJ noise on {street}"),
    "construction": ("Debris from building work dumped near {landmark}", "Digging work left open on {street}", "Rubble and sand blocking {street}",
              "Construction debris dumped on {street}", "Building material blocking road near {landmark}",
                     "Excavation left open without barricade at {street}"),
}
_STREETS = ("MG Road", "Station Road", "Main Street", "2nd Cross", "Ring Road", "Market Lane", "Temple Street", "College Road",
            "Lake View Road", "Gandhi Nagar 5th Main", "Nehru Marg", "Industrial Area Phase 2")
_LANDMARKS = ("the bus stand", "the government school", "the metro station", "the vegetable market", "the hospital gate",
              "the park", "the temple", "the post office", "the water tank", "the railway crossing")
_NOISE = ("", " Please do something urgently.", " Complained before, no action.", " Kindly resolve.", " Very dangerous.",
          " Residents are suffering.", "")


def synthesize(n: int, seed: int = 7) -> list[dict[str, str]]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        cat = CATEGORIES[i % len(CATEGORIES)] if i < len(CATEGORIES) * 4 else rng.choice(CATEGORIES)
        tid = rng.randrange(len(_TEMPLATES[cat]))
        text = _TEMPLATES[cat][tid].format(street=rng.choice(_STREETS), landmark=rng.choice(_LANDMARKS), n=rng.randint(2, 9))
        text += rng.choice(_NOISE)
        if rng.random() < 0.15:
            text = text.lower()
        rows.append({"text": text, "category": cat, "template_id": f"{cat}:{tid}"})
    return rows


def rule_classify(text: str) -> tuple[str, float]:
    t = text.lower()
    scores = {cat: sum(1 for k in kws if k in t) for cat, kws in KEYWORDS.items()}
    best = max(scores, key=lambda c: scores[c])
    total = sum(scores.values())
    if scores[best] == 0:
        return "roads", 0.0  # no evidence: lowest-confidence default, flagged by confidence 0
    return best, round(scores[best] / total, 2)


class KeywordLexiconFeatures:
    """Sklearn-compatible transformer: per-category keyword hit counts (domain-knowledge features)."""

    def fit(self, X, y=None):  # type: ignore[no-untyped-def]  # noqa: N803
        return self

    def transform(self, X):  # type: ignore[no-untyped-def]  # noqa: N803
        import numpy as np
        from scipy.sparse import csr_matrix

        rows = []
        for text in X:
            t = str(text).lower()
            rows.append([float(sum(1 for k in KEYWORDS[c] if k in t)) for c in CATEGORIES])
        return csr_matrix(np.array(rows) * 2.0)

    def get_params(self, deep: bool = True) -> dict:
        return {}

    def set_params(self, **params):  # type: ignore[no-untyped-def]
        return self
