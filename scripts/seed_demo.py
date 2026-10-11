"""Load the demo: a finalised RFQ (81 lines, 9 questions) and 8 fabricated vendor proposals.

    python -m scripts.seed_demo

Creates a new RFQ in STATE_DIR/live and uploads each proposal under its organisation name. Then open
Compare quotes and press Run comparison. The proposals are fabricated for this demo.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from app import config, live_rfx as L, printing
from app.util import iso

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PROPOSALS = {
    "Apex Technologies": "Apex Technologies Proposal.png",
    "Best Tech": "Best Tech - Commercial Proposal & RFQ Response.pdf",
    "Brightline Tech Supplies": "Brightline Tech Supplies Proposal.pdf",
    "IT Hub Karnal": "IT_Hub_Karnal_RFQ_Response.pdf",
    "Maratha Tech Hub": "Maratha Tech Hub - Proposal.pdf",
    "NexGen Systems": "NexGen Systems Proposal.png",
    "Reliable Tech Solutions": "Reliable Tech Solutions Proposal.pdf",
    "Startup Solutions": "Startup Solutions Proposal.png",
}


def main() -> int:
    st = L.create_rfq()
    st["rfx"] = json.loads((SAMPLES / "rfq-sample.json").read_text())
    st["status"], st["finalised_at"] = "FINAL", iso(config.real_now())
    st["share_token"] = L.secrets.token_urlsafe(16)
    try:
        printing.print_pdf(L.rfq_html(L.view(st)), L.rdir(st["id"]) / "rfq.pdf")   # same PDF the app would produce
    except printing.PdfError as e:
        print(f"Chrome isn't available ({e}); using the sample PDF instead.")
        L.rdir(st["id"]).mkdir(parents=True, exist_ok=True)
        shutil.copy(SAMPLES / "RFQ-sample.pdf", L.rdir(st["id"]) / "rfq.pdf")
    L.save(st)
    for org, name in PROPOSALS.items():
        L.store_upload(st["id"], org, None, "buyer", name, (SAMPLES / "proposals" / name).read_bytes())
    L.set_current(st["id"])
    print(f"Created {st['id']} with {len(PROPOSALS)} proposals. Open http://localhost:8000/web/#/compare-live?id={st['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
