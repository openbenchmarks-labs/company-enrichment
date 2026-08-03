from . import (
    apollo,
    company_enrich,
    exa_research_v2,
    explorium,
    parallel_research,
    people_data_labs,
    predictleads,
)


REGISTRY = {
    predictleads.VENDOR_SLUG: predictleads,
    explorium.VENDOR_SLUG: explorium,
    apollo.VENDOR_SLUG: apollo,
    people_data_labs.VENDOR_SLUG: people_data_labs,
    company_enrich.VENDOR_SLUG: company_enrich,
    exa_research_v2.VENDOR_SLUG: exa_research_v2,
    parallel_research.VENDOR_SLUG: parallel_research,
}
