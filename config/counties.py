"""
SurplusIQ — County Configuration (v4 — VA AUDIT APPLIED)
All URLs and workflows verified by VA against Loom videos.
"""

from dataclasses import dataclass, field
from typing import Literal


# ═══════════════════════════════════════════════════════════════════════
# Lead window — SINGLE SOURCE OF TRUTH
# ═══════════════════════════════════════════════════════════════════════
# One number governs both sides of the pipeline:
#   • auction scrape depth  (core/auction/universal.py --days default)
#   • lead display window   (core/loader.py window_days,
#                            core/dashboard_data.py killed-leads cutoff)
# They MUST be the same value: every lead still displayed is re-scraped
# (auction + docket) on each daily run, so late-filed kill signals —
# vacated sales, surplus claims — are caught for the lead's whole life.
# History: scrape ran at 14d while the loader kept 28d, so days 15-28
# were displayed but never re-verified (audit finding 1.5).
LEAD_WINDOW_DAYS = 28

# Confirmed-tier leads (docket-proven surplus, e.g. a certificate of
# disbursement) are the actual deliverable and must NOT age out on the standard
# clock before they can be pursued. They are retained for a full quarter:
#   • past the FL 60-day junior-lienholder claim window (FS 45.032) — after
#     which the homeowner's own recovery window is the live opportunity — with
#     working margin to locate the owner and file;
#   • well within the escheat timeline (FL reports unclaimed surplus after ~1yr;
#     OH escheats after years), so a 90-day retention never shows funds that
#     have already left the clerk.
# 90d ≈ 3× the standard window. Apparent / estimated / unverified leads keep
# LEAD_WINDOW_DAYS unchanged — only confirmed leads get the longer window.
CONFIRMED_WINDOW_DAYS = 90


@dataclass
class CountyConfig:
    id:              str
    name:            str
    state:           str
    state_full:      str
    auction_url:     str
    auction_platform: str
    auction_preview_url: str = ""
    auction_calendar_url: str = ""
    auction_results_pattern: str = ""
    clerk_url:       str = ""
    clerk_search_url: str = ""
    clerk_system:    str = ""
    case_format:     str = ""
    case_format_example: str = ""
    case_search_method: Literal["paste_full", "split_fields", "paste_with_hyphens"] = "paste_full"
    tax_deed_url:    str = ""
    tax_deed_system: str = ""
    has_captcha:     bool = False
    vpn_blocked:     bool = False
    requires_terms_agreement: bool = False
    doc_timing_days: int = 10
    sale_days:       list = field(default_factory=list)
    mortgage_tax_separated: bool = False
    is_two_tier:     bool = False
    debt_lookup_method: str = ""
    recorder_url:    str = ""
    property_appraiser_url: str = ""
    tax_bill_search_url: str = ""
    claim_form_url:  str = ""
    excess_funds_list_url: str = ""
    lead_types:      list = field(default_factory=lambda: ["Mortgage Foreclosure"])
    notes:           str = ""


# ═══════════════════════════════════════════════════════════════════════
# FLORIDA — 5 COUNTIES (VA-verified)
# ═══════════════════════════════════════════════════════════════════════

MIAMI_DADE = CountyConfig(
    id="miami-dade-fl", name="Miami-Dade", state="FL", state_full="Florida",
    auction_url="https://www.miamidade.realforeclose.com/index.cfm",
    auction_platform="realforeclose",
    auction_calendar_url="https://www.miamidade.realforeclose.com/index.cfm",
    auction_results_pattern="https://www.miamidade.realforeclose.com/index.cfm?zaction=AUCTION&Zmethod=PREVIEW",
    clerk_url="https://www.miamidadeclerk.gov/clerk/home.page",
    clerk_search_url="http://www2.miamidadeclerk.gov/ocs/",
    clerk_system="oscar",
    case_format=r"\d{4}-[A-Z]{2,4}-\d{4,}",
    case_format_example="2025-CA-048821",
    case_search_method="split_fields",
    tax_deed_url="https://miamidade.realtdm.com/public/cases/list",
    tax_deed_system="realtdm",
    doc_timing_days=14,
    recorder_url="https://www.miamidadeclerk.gov/clerk/official-records.page",
    property_appraiser_url="http://www.miamidade.gov/pa/property_search.asp",
    tax_bill_search_url="https://www.miamidade.gov/global/taxcollector/home.page",
    lead_types=["Mortgage Foreclosure", "Tax Deed", "HOA Foreclosure"],
    notes=(
        "Case # MUST be split into 3 fields (year + sequence + code). "
        "CA = mortgage, CC = HOA. Tax deeds use separate RealTDM portal. "
        "Both types on same auction calendar. Cert of Disbursement = surplus proof. "
        "Surplus Letter + PIR = tax deed surplus proof."
    ),
)

BROWARD = CountyConfig(
    id="broward-fl", name="Broward", state="FL", state_full="Florida",
    auction_url="https://broward.realforeclose.com/index.cfm",
    auction_platform="realforeclose",
    auction_calendar_url="https://broward.realforeclose.com/index.cfm",
    clerk_url="https://www.browardclerk.org/",
    clerk_search_url="https://www.browardclerk.org/Web2/CaseSearchECA/Index/?AccessLevel=ANONYMOUS",
    clerk_system="broward_web2",
    case_format=r"\d{4}-[A-Z]{2,4}-\d{4,}",
    case_format_example="CACE-24-012345",
    case_search_method="paste_full",
    tax_deed_url="https://broward.deedauction.net/reports/total_sales",
    tax_deed_system="broward_deedauction",
    has_captcha=True,
    doc_timing_days=7,
    mortgage_tax_separated=True,
    recorder_url="https://www.broward.org/RecordsTaxesTreasury/Records/pages/publicrecordssearch.aspx",
    property_appraiser_url="https://bcpa.net/RecMenu.asp",
    tax_bill_search_url="https://broward.county-taxes.com/public/search/property_tax",
    lead_types=["Mortgage Foreclosure", "Tax Deed"],
    notes=(
        "CAPTCHA on clerk. Paste full case number. "
        "Tax deed on SEPARATE system: broward.deedauction.net. "
        "KILL: Motion to Cancel, Order Vacating, Bankruptcy. "
        "HIGH COMPETITION: Motion to Intervene = another recovery co."
    ),
)

DUVAL = CountyConfig(
    id="duval-fl", name="Duval", state="FL", state_full="Florida",
    auction_url="https://www.duval.realforeclose.com/index.cfm",
    auction_platform="realforeclose",
    auction_calendar_url="https://www.duval.realforeclose.com/index.cfm",
    clerk_url="https://www.duvalclerk.com/",
    clerk_search_url="https://core.duvalclerk.com/CoreCms.aspx?mode=PublicAccess",
    clerk_system="core_duval",
    case_format=r"\d{2}-\d{4}-[A-Z]{2}-\d{6,}",
    case_format_example="16-2024-CA-001234",
    case_search_method="paste_full",
    tax_deed_url="https://taxdeed.duvalclerk.com/",
    tax_deed_system="duval_taxdeed",
    has_captcha=True,
    doc_timing_days=5,
    mortgage_tax_separated=True,
    recorder_url="https://www.duvalclerk.com/departments/county-services/official-records-and-research",
    property_appraiser_url="https://paopropertysearch.coj.net/Basic/Search.aspx",
    tax_bill_search_url="https://duval.county-taxes.com/public/search/property_tax",
    lead_types=["Mortgage Foreclosure", "Tax Deed"],
    notes=(
        "Mortgage + tax deed on SEPARATE systems. CORE clerk portal. "
        "Tax deed portal shows surplus directly. "
        "OE Report / Title Express for tax deed liens."
    ),
)

LEE = CountyConfig(
    id="lee-fl", name="Lee", state="FL", state_full="Florida",
    auction_url="https://www.lee.realforeclose.com/index.cfm",
    auction_platform="realforeclose",
    auction_calendar_url="https://www.lee.realforeclose.com/index.cfm",
    clerk_url="https://www.leeclerk.org/",
    clerk_search_url="https://matrix.leeclerk.org/",
    clerk_system="lee_olo",
    case_format=r"\d{2}-[A-Z]{2,4}-\d{6,}",
    case_format_example="25-CA-012345",
    case_search_method="paste_full",
    tax_deed_url="https://www.lee.realtaxdeed.com/",
    tax_deed_system="lee_realtaxdeed",
    has_captcha=True,
    doc_timing_days=7,
    mortgage_tax_separated=True,
    recorder_url="https://officialrecords.leeclerk.org/",
    property_appraiser_url="https://www.leepa.org/",
    tax_bill_search_url="https://www.leetc.com/",
    lead_types=["Mortgage Foreclosure", "Tax Deed"],
    notes=(
        "Same pattern as Orange per VA. CAPTCHA on clerk search. "
        "Paste full case number. Mortgage + tax deed on separate systems. "
        "Standard Florida kill/valid/surplus signals apply."
    ),
)

ORANGE = CountyConfig(
    id="orange-fl", name="Orange", state="FL", state_full="Florida",
    auction_url="https://myorangeclerk.realforeclose.com/index.cfm",
    auction_platform="realforeclose",
    auction_calendar_url="https://myorangeclerk.realforeclose.com/index.cfm",
    clerk_url="https://myorangeclerk.com/",
    clerk_search_url="https://myeclerk.myorangeclerk.com/",
    clerk_system="orange_eclerk",
    case_format=r"\d{4}-[A-Z]{2,4}-\d{4,}",
    case_format_example="2025-CA-001234-O",
    case_search_method="paste_full",
    tax_deed_url="https://or.occompt.com/recorder/web/login.jsp",
    tax_deed_system="orange_comptroller",
    has_captcha=True,
    vpn_blocked=True,
    doc_timing_days=2,
    mortgage_tax_separated=True,
    recorder_url="https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH29SOS1",
    property_appraiser_url="https://ocpaweb.ocpafl.org/dashboard",
    tax_bill_search_url="https://www.octaxcol.com/taxes/about-property-tax/",
    lead_types=["Mortgage Foreclosure", "Tax Deed"],
    notes=(
        "Auction URL is myorangeclerk.realforeclose.com (NOT orange.realforeclose.com). "
        "VPN MUST BE OFF. CAPTCHA. Fastest doc posting (2 days). "
        "Tax deed: 'Active Overbid' status = confirmed surplus. "
        "Click Overbid → View Claims → Notice of Surplus + claim form."
    ),
)

# ═══════════════════════════════════════════════════════════════════════
# OHIO — all on sheriffsaleauction.ohio.gov (NOT realforeclose!)
# ═══════════════════════════════════════════════════════════════════════

CUYAHOGA = CountyConfig(
    id="cuyahoga-oh", name="Cuyahoga", state="OH", state_full="Ohio",
    auction_url="https://cuyahoga.sheriffsaleauction.ohio.gov/",
    auction_platform="sheriffsaleauction",
    auction_calendar_url="https://cuyahoga.sheriffsaleauction.ohio.gov/index.cfm?zaction=USER&zmethod=CALENDAR",
    auction_preview_url="https://cuyahoga.sheriffsaleauction.ohio.gov/index.cfm?zaction=AUCTION&zmethod=PREVIEW",
    clerk_url="https://cpdocket.cp.cuyahogacounty.gov/",
    clerk_search_url="https://cpdocket.cp.cuyahogacounty.gov/",
    clerk_system="cuyahoga_docket",
    case_format=r"CV-\d{2}-\d{6,}",
    case_format_example="CV-23-987654",
    case_search_method="split_fields",
    is_two_tier=True,
    debt_lookup_method="prayer_amount",
    doc_timing_days=10,
    sale_days=["monday", "wednesday"],
    mortgage_tax_separated=True,
    excess_funds_list_url="https://cuyahogacounty.gov/coc/excess-funds",
    lead_types=["Mortgage Foreclosure", "Tax Sale"],
    notes=(
        "Auction domain: sheriffsaleauction.ohio.gov (NOT realforeclose). "
        "TWO-TIER. Mon=mortgage, Wed=tax. Split search fields. "
        "Search docket for 'Prayer Amount' = actual debt. "
        "'Clerk to Hold Sale Funds' = surplus confirmation. "
        "Public excess funds page: cuyahogacounty.gov/coc/excess-funds."
    ),
)

FRANKLIN = CountyConfig(
    id="franklin-oh", name="Franklin", state="OH", state_full="Ohio",
    auction_url="https://franklin.sheriffsaleauction.ohio.gov/",
    auction_platform="sheriffsaleauction",
    auction_calendar_url="https://franklin.sheriffsaleauction.ohio.gov/index.cfm?zaction=USER&zmethod=CALENDAR",
    auction_preview_url="https://franklin.sheriffsaleauction.ohio.gov/index.cfm?zaction=AUCTION&zmethod=PREVIEW",
    clerk_url="https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline/",
    clerk_search_url="https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline/",
    clerk_system="franklin_cio",
    case_format=r"\d{2}CV\d{6,}",
    case_format_example="25CV001234",
    case_search_method="paste_full",
    has_captcha=True,
    is_two_tier=True,
    debt_lookup_method="judgment_search",
    doc_timing_days=10,
    lead_types=["Mortgage Foreclosure"],
    notes=(
        "Auction domain: sheriffsaleauction.ohio.gov. TWO-TIER. "
        "Most transparent OH county — posts 'Notice of Excess Proceeds'. "
        "Search docket for 'judgment' (J-U-D-G-M-E-N-T) for debt. "
        "'Motion to Confirm Sale GRANTED' = valid sale. "
        "Holds ~$7.3M+ in excess proceeds."
    ),
)

MONTGOMERY = CountyConfig(
    id="montgomery-oh", name="Montgomery", state="OH", state_full="Ohio",
    auction_url="https://montgomery.sheriffsaleauction.ohio.gov/",
    auction_platform="sheriffsaleauction",
    auction_preview_url="https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?zaction=AUCTION&zmethod=PREVIEW",
    clerk_url="https://pro.mcohio.org",
    clerk_search_url="https://pro.mcohio.org",
    clerk_system="montgomery_pro",
    case_format=r"\d{4}\s?CV\s?\d{4,}",
    case_format_example="2025 CV 04065",
    case_search_method="paste_full",
    has_captcha=True,
    requires_terms_agreement=True,
    is_two_tier=True,
    debt_lookup_method="judgment_search",
    doc_timing_days=10,
    sale_days=["friday"],
    lead_types=["Mortgage Foreclosure"],
    notes=(
        "Auction domain: sheriffsaleauction.ohio.gov. TWO-TIER. "
        "Sales ONLY Fridays. Must agree to T&C first. "
        "Format requires spaces: '2025 CV 04065'. "
        "Cmd-F 'judgment' for debt. Motion to Vacate = kill. "
        "~$5.8M held excess funds."
    ),
)

SUMMIT = CountyConfig(
    id="summit-oh", name="Summit", state="OH", state_full="Ohio",
    auction_url="https://summit.sheriffsaleauction.ohio.gov/",
    auction_platform="sheriffsaleauction",
    auction_calendar_url="https://summit.sheriffsaleauction.ohio.gov/index.cfm?zaction=USER&zmethod=CALENDAR",
    auction_preview_url="https://summit.sheriffsaleauction.ohio.gov/index.cfm?zaction=AUCTION&zmethod=PREVIEW",
    clerk_url="https://clerk.summitoh.net/PublicSite/Home.aspx",
    clerk_search_url="https://clerk.summitoh.net/PublicSite/SearchByCaseNbrCivil.aspx",
    clerk_system="summit_publicsite",
    case_format=r"CV-?\d{4}-?\d{2}-?\d{4,}",
    case_format_example="CV-2025-03-1239",
    case_search_method="split_fields",
    has_captcha=True,
    is_two_tier=True,
    debt_lookup_method="judgment_search",
    doc_timing_days=10,
    lead_types=["Mortgage Foreclosure"],
    notes=(
        "Auction domain: sheriffsaleauction.ohio.gov. TWO-TIER. "
        "STRICT format: CV-YYYY-MM-####. Split fields. "
        "Judgment often UNCLEAR — lean on PropertyRadar. "
        "'Excess funds sent to fiscal office' = surplus signal. "
        "Clerk Disclaimer page required first."
    ),
)

HAMILTON = CountyConfig(
    id="hamilton-oh", name="Hamilton", state="OH", state_full="Ohio",
    auction_url="https://hamilton.sheriffsaleauction.ohio.gov/",
    auction_platform="sheriffsaleauction",
    auction_preview_url="https://hamilton.sheriffsaleauction.ohio.gov/index.cfm?zaction=AUCTION&zmethod=PREVIEW",
    clerk_url="https://courtclerk.org/",
    clerk_search_url="https://courtclerk.org/records-search/case-number-search/",
    clerk_system="hamilton_courtclerk",
    case_format=r"[A-Z]\s?\d{7,}",
    case_format_example="A 2502173",
    case_search_method="paste_full",
    has_captcha=True,
    is_two_tier=True,
    debt_lookup_method="propertyradar_primary",
    doc_timing_days=10,
    sale_days=["twice_per_month"],
    excess_funds_list_url="https://courtclerk.org/forms/excess_funds_list.pdf",
    lead_types=["Mortgage Foreclosure"],
    notes=(
        "Auction domain: sheriffsaleauction.ohio.gov. TWO-TIER. "
        "Format: 'A 2502173' with space. Twice/month sales. "
        "BOTTLENECK: docket docs NOT LABELED. "
        "STRATEGY: Skip docket parsing, go address-first via PropertyRadar. "
        "HUD + 3rd-position liens common. "
        "Excess funds is a PDF: courtclerk.org/forms/excess_funds_list.pdf"
    ),
)

# ═══════════════════════════════════════════════════════════════════════
# REGISTRY
# ═══════════════════════════════════════════════════════════════════════

ALL_COUNTIES = [
    MIAMI_DADE, BROWARD, DUVAL, LEE, ORANGE,
    CUYAHOGA, FRANKLIN, MONTGOMERY, SUMMIT, HAMILTON,
]
FL_COUNTIES = [c for c in ALL_COUNTIES if c.state == "FL"]
OH_COUNTIES = [c for c in ALL_COUNTIES if c.state == "OH"]
COUNTY_BY_ID = {c.id: c for c in ALL_COUNTIES}
CAPTCHA_COUNTIES = [c.id for c in ALL_COUNTIES if c.has_captcha]
REALFORECLOSE_COUNTIES = [c.id for c in ALL_COUNTIES if c.auction_platform == "realforeclose"]
SHERIFFSALE_COUNTIES   = [c.id for c in ALL_COUNTIES if c.auction_platform == "sheriffsaleauction"]


def get_county(county_id: str) -> CountyConfig:
    return COUNTY_BY_ID[county_id]


def needs_verification(county: CountyConfig) -> list:
    gaps = []
    if not county.clerk_search_url:       gaps.append("clerk_search_url")
    if county.state == "FL" and county.mortgage_tax_separated and not county.tax_deed_url:
        gaps.append("tax_deed_url")
    return gaps


if __name__ == "__main__":
    print("\n" + "="*78)
    print("  SurplusIQ County Configuration — VA AUDIT APPLIED")
    print("="*78)
    print(f"\nTotal: {len(ALL_COUNTIES)} | FL: {len(FL_COUNTIES)} | OH: {len(OH_COUNTIES)}")
    print(f"Platforms: RealForeclose ({len(REALFORECLOSE_COUNTIES)}) | SheriffSaleAuction ({len(SHERIFFSALE_COUNTIES)})")
    print(f"CAPTCHA counties: {len(CAPTCHA_COUNTIES)}")
    print(f"\n{'County':<14} {'Platform':<22} {'Flags':<26} {'Status'}")
    print("-"*78)
    for c in ALL_COUNTIES:
        gaps = needs_verification(c)
        flags = []
        if c.has_captcha: flags.append("CAPTCHA")
        if c.vpn_blocked: flags.append("NO-VPN")
        if c.is_two_tier: flags.append("2-TIER")
        if c.requires_terms_agreement: flags.append("T&C")
        flag_str = ",".join(flags) if flags else "-"
        status = "✅" if not gaps else f"⚠️ {','.join(gaps)}"
        print(f"  {c.state} {c.name:<11} {c.auction_platform:<22} {flag_str:<26} {status}")
    print()
