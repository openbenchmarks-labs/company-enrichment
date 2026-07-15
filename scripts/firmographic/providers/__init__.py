from . import (
    apollo,
    company_enrich,
    explorium,
    fiber,
    ocean_enrichment,
    people_data_labs,
    predictleads,
)


REGISTRY = {
    predictleads.VENDOR_SLUG: predictleads,
    fiber.VENDOR_SLUG: fiber,
    explorium.VENDOR_SLUG: explorium,
    apollo.VENDOR_SLUG: apollo,
    people_data_labs.VENDOR_SLUG: people_data_labs,
    ocean_enrichment.VENDOR_SLUG: ocean_enrichment,
    company_enrich.VENDOR_SLUG: company_enrich,
}
