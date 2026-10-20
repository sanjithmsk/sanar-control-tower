"""Pharmacy pack: the Sanar network. FEFO, cold chain, controlled substances, SFDA imports."""
from .base import Domain, Requirement

SAMPLE = {
    # code, name, customs clearance days for imports into the country
    "countries": [("SA", "Saudi Arabia", 2), ("DE", "Germany", 1)],
    # id, name, kind, country, capabilities, storage capacity in units (None = not tracked)
    "sites": [
        ("A", "Central Retail", "branch", "SA", "cold,controlled,hazmat", None),
        ("B", "North Branch", "branch", "SA", "cold,controlled,hazmat", None),
        ("C", "East Branch", "branch", "SA", "cold,controlled", None),
        ("D", "Distribution Hub", "hub", "SA", "cold,controlled,hazmat", None),
    ],
    # id, name, category, tags, seasonal, clinical impact 1-10, pack, unit price, shelf life days, HS code, duty %
    "items": [
        ("INS", "Insulin glargine 100 IU/mL", "medicine", "cold,regulated", 0, 9, 10, 85, 365, "3004.31", 0),
        ("ADA", "Adalimumab 40 mg pen", "medicine", "cold,regulated", 0, 8, 2, 2400, 365, "3002.15", 0),
        ("TRM", "Tramadol 50 mg", "medicine", "controlled,regulated", 0, 7, 10, 12, 730, "3004.49", 0),
        ("OSE", "Oseltamivir 75 mg", "medicine", "regulated", 1, 7, 10, 28, 730, "3004.90", 0),
        ("AMX", "Amoxicillin 500 mg", "medicine", "regulated", 0, 6, 20, 4, 730, "3004.10", 0),
        ("PCM", "Paracetamol 500 mg", "medicine", "regulated", 1, 4, 50, 1, 1095, "3004.90", 0),
        ("INB", "Insulin glargine biosimilar 100 IU/mL", "medicine", "cold,regulated", 0, 9, 10, 78, 365, "3004.31", 0),
        ("GLM", "Blood glucose meter", "device", "regulated", 0, 5, 5, 120, None, "9027.80", 5),
        ("HSN", "Hand sanitizer 500 mL (70% alcohol)", "consumer", "hazmat", 1, 3, 12, 15, 730, "3808.94", 5),
    ],
    # item -> site -> (lots as (qty, days to expiry or None), 14-day forecast, safety buffer)
    "stock": {
        "INS": {"A": ([(40, 200)], 120, 30), "B": ([(160, 90), (100, 240)], 90, 30),
                "C": ([(110, 150)], 80, 25), "D": ([(150, 300)], 60, 60)},
        "ADA": {"A": ([(2, 120)], 8, 2), "B": ([(6, 200)], 5, 1), "C": ([(4, 160)], 3, 1), "D": ([(14, 270)], 4, 4)},
        "TRM": {"A": ([(70, 300)], 50, 20), "B": ([(20, 150)], 60, 20), "C": ([(55, 200)], 40, 15),
                "D": ([(100, 365)], 60, 40)},
        "OSE": {"A": ([(150, 220)], 100, 30), "B": ([(120, 240)], 80, 25), "C": ([(100, 200), (300, 45)], 70, 20),
                "D": ([(400, 300)], 150, 80)},
        "AMX": {"A": ([(420, 180), (40, -3)], 280, 60), "B": ([(80, 45), (300, 120)], 250, 50),
                "C": ([(150, 210)], 300, 60), "D": ([(900, 330)], 300, 100)},
        "PCM": {"A": ([(900, 300)], 600, 150), "B": ([(700, 250)], 500, 120),
                "C": ([(300, 20), (520, 280)], 400, 100), "D": ([(3000, 400)], 1000, 500)},
        "INB": {"D": ([(60, 280)], 28, 10)},
        "GLM": {"A": ([(40, None)], 28, 8), "B": ([(20, None)], 14, 6), "C": ([(12, None)], 14, 10)},
        "HSN": {"A": ([(200, 500)], 140, 40), "B": ([(150, 480)], 100, 30), "D": ([(600, 520)], 200, 100)},
    },
    # a, b, mode, transit hours, capabilities of the vehicles on the lane
    "lanes": [("A", "B", "road", 2, "cold,hazmat"), ("A", "C", "road", 3, "cold"), ("A", "D", "road", 4, "cold,hazmat"),
              ("B", "C", "road", 3, ""), ("B", "D", "road", 5, "cold,hazmat"), ("C", "D", "road", 2, "cold")],
    # name, country, lead days, price multiplier, freight %, compliance %, primary, certifications, categories, incoterm
    "suppliers": [
        ("Supplier A (primary)", "SA", 3, 1.0, 0, 98, 1, "cold,controlled,hazmat", "medicine,consumer", "DDP"),
        ("Supplier B (secondary)", "SA", 5, 1.08, 0, 94, 0, "cold,controlled,hazmat", "medicine,device,consumer", "DDP"),
        ("Supplier C (spot wholesaler)", "SA", 2, 1.25, 0, 85, 0, "", "medicine,consumer", "DDP"),
        ("MedTech GmbH (Germany)", "DE", 7, 0.80, 6, 97, 0, "", "device", "FCA"),
    ],
    # permit reference, item category, importing country, valid for days
    "permits": [("SFDA-MDIP-2026-0147", "device", "SA", 300)],
    "substitutes": [("INS", "INB", "Biosimilar insulin glargine. Interchange needs pharmacist sign-off "
                                   "and prescriber notification.")],
    "scenarios": {
        "cold_fail": ("Cold-chain failure on routes to Central Retail", {"type": "cap_outage", "site": "A", "cap": "cold"}),
        "supplier_short": ("Primary supplier shortage", {"type": "primary_supplier_out"}),
        "surge": ("Seasonal flu surge", {"type": "demand_surge"}),
        "route_closure": ("Road closure Central Retail – North Branch", {"type": "lane_closed", "lane": ["A", "B"]}),
        "hub_outage": ("Distribution Hub dispatch outage", {"type": "site_down", "site": "D"}),
        "customs_delay": ("Customs delays on imports (+5 days)", {"type": "customs_delay", "days": 5}),
    },
}

PHARMACY = Domain(
    name="pharmacy",
    title="Sanar Pharmacy",
    tagline="Pharmacy network · FEFO, cold chain, controlled substances, recalls and SFDA imports",
    labels={"item": "Medicine", "items": "Medicines", "site": "Branch", "sites": "Branches", "lot": "Batch",
            "demand": "Dispensing"},
    roles={"pharmacist": "Pharmacist", "chief_pharmacist": "Chief pharmacist",
           "logistics_manager": "Logistics manager", "qa": "QA officer"},
    approvals={
        "value": ("chief_pharmacist", "logistics_manager"),
        "price_premium": ("logistics_manager", "chief_pharmacist"),
        "max_cover": ("logistics_manager", "chief_pharmacist"),
        "budget": ("pharmacist", "chief_pharmacist", "logistics_manager"),
        "release": ("qa", "chief_pharmacist"),
        "dispose": ("qa", "pharmacist", "chief_pharmacist"),
        "recall": ("qa", "chief_pharmacist"),
        "policy": ("chief_pharmacist", "logistics_manager"),
    },
    requirements={
        "cold": Requirement("Cold chain 2–8 °C", "2–8 °C", site=True, lane=True, supplier=True, temp=(2, 8),
                            tolerance_min=30, freeze_below=0, ref="SOP-CC-01"),
        "controlled": Requirement("Controlled substance", "Controlled", site=True, supplier=True,
                                  signoff=("pharmacist", "chief_pharmacist"), ref="POL-CS-03"),
        "hazmat": Requirement("Flammable (dangerous goods)", "Flammable", site=True, lane=True, supplier=True,
                              ref="SOP-DG-01"),
        "regulated": Requirement("SFDA-registered product", permit=True, ref="POL-SFDA-01"),
    },
    refs={"approvals": "POL-CS-03", "temperature": "SOP-CC-01"},
    sample=SAMPLE,
)
