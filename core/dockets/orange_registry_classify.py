"""
Orange (FL) Court Registry Balance — lifecycle-stage classifier (pure, testable).

The clerk's Court Registry Balance endpoint returns the CURRENT held-funds balance
for a case's registry account. That number is NOT a day-one surplus figure — it's a
point-in-time balance that moves through the foreclosure lifecycle. This module maps
a (validated) registry lookup + the auction winning bid to a lifecycle STAGE.

Staged by BALANCE-vs-BID, never by elapsed time (the distribution lag varies):

  1. no registry account         → KILL. Funds are gone (disbursed, claimed, or
     never held). This is Orange's only positive verification signal today.
  2. balance ≈ full winning bid  → PENDING distribution. Proceeds deposited, court
     hasn't distributed to the plaintiff yet. NOT a surplus figure — never display
     it as one, never let it inflate a total.
  3. balance well below the bid  → DISTRIBUTED. The plaintiff was paid; the balance
     IS the clerk-stated held surplus. Use in place of auction math.

Case type (from the case number, e.g. 2025-CA-003157-O):
  • CA circuit mortgage  → a distributed balance is a CLEAN owner surplus
    (confirmed-tier eligible).
  • CC county-court HOA  → the registry genuinely HOLDS the money (2023-CC-006558-O
    returned $194,881 vs $192,619 apparent), but a surviving senior mortgage clouds
    the OWNER's claim. Shown as real held funds WITH the HOA caution; not booked as
    clean owner surplus. NOT phantom — the funds exist.

The transport (orange_registry.lookup_registry) NEVER calls this on a failed /
challenged / unparseable lookup — a valid, parsed result is a precondition. Fail
loud upstream; this module never fabricates or infers a balance.
"""
from __future__ import annotations
import re

STAGE_KILL = "no_registry"
STAGE_PENDING = "pending_distribution"
STAGE_DISTRIBUTED = "distributed"
STAGE_UNSTAGED = "balance_found_unstaged"


def case_type(case_number: str) -> str:
    """The 2-letter division from an Orange case number ('2025-CA-003157-O' → 'CA',
    '2025-CC-017428-O' → 'CC'). '' if not parseable."""
    m = re.search(r"-([A-Z]{2})-", (case_number or "").upper())
    return m.group(1) if m else ""


def classify_registry(lookup: dict, final_sale_price, opening_bid=0.0,
                      case_number: str = "") -> dict:
    """Map a VALIDATED registry lookup + the winning bid to a lifecycle stage.

    lookup: {found: bool, balance: float|None, registry_type: str, as_of: str}
            — the transport guarantees this is a parsed, non-error result.

    Returns a dict of registry fields for the docket record:
      registry_status  : no_registry | pending_distribution | distributed |
                         balance_found_unstaged
      registry_balance : float | None
      registry_as_of   : str
      kill             : bool   (only STAGE_KILL)
      kill_signal      : str    (only on kill)
      confirmed_eligible: bool  (only CA distributed — clean owner surplus)
      hoa_caution      : bool   (CC — senior mortgage clouds owner recovery)
      reason           : str
    Never returns a fabricated/inferred balance.
    """
    ct = case_type(case_number)
    is_cc = (ct == "CC")
    bid = float(final_sale_price or 0.0)

    # 1 — no registry account → KILL.
    if not lookup.get("found"):
        return {
            "registry_status": STAGE_KILL, "registry_balance": None, "registry_as_of": "",
            "kill": True, "kill_signal": "registry_funds_gone",
            "confirmed_eligible": False, "hoa_caution": is_cc,
            "reason": "clerk registry: no associated account — surplus disbursed, "
                      "claimed, or never held",
        }

    balance = lookup.get("balance")
    as_of = lookup.get("as_of", "") or ""
    if balance is None:
        # A "found" result must carry a balance; a valid lookup never reaches here
        # without one. Surface as unstaged rather than guess.
        return {
            "registry_status": STAGE_UNSTAGED, "registry_balance": None,
            "registry_as_of": as_of, "kill": False, "confirmed_eligible": False,
            "hoa_caution": is_cc,
            "reason": "clerk registry: account present but balance unparseable — not staged",
        }

    balance = float(balance)

    # Can't stage without a bid to compare against — surface the balance but never
    # claim distributed/confirmed (fail safe).
    if bid <= 0:
        return {
            "registry_status": STAGE_UNSTAGED, "registry_balance": balance,
            "registry_as_of": as_of, "kill": False, "confirmed_eligible": False,
            "hoa_caution": is_cc,
            "reason": f"clerk registry balance ${balance:,.0f} as of {as_of} — "
                      f"winning bid unknown, stage indeterminate",
        }

    # 2 — balance ≈ full winning bid → pending distribution. Tolerance: within 2% or
    #     $2,000 of the bid (a small clerk fee is not a distribution).
    if (bid - balance) <= max(0.02 * bid, 2000.0):
        return {
            "registry_status": STAGE_PENDING, "registry_balance": balance,
            "registry_as_of": as_of, "kill": False, "confirmed_eligible": False,
            "hoa_caution": is_cc,
            "reason": f"clerk registry: full sale proceeds deposited "
                      f"(${balance:,.0f} ≈ winning bid) — distribution pending as of "
                      f"{as_of}. NOT a surplus figure.",
        }

    # 3 — balance well below the bid → distributed; balance IS the held surplus.
    return {
        "registry_status": STAGE_DISTRIBUTED, "registry_balance": balance,
        "registry_as_of": as_of, "kill": False,
        "confirmed_eligible": (not is_cc), "hoa_caution": is_cc,
        "reason": (
            f"clerk registry: held surplus ${balance:,.0f} as of {as_of}"
            + (" — county-court/HOA foreclosure: funds are real but a surviving "
               "senior mortgage may cloud owner recovery"
               if is_cc else
               " — circuit mortgage foreclosure: clean owner surplus")),
    }
