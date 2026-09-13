# Where our answer and the reference disagree, with the page evidence

Fifteen of 1,329 field answers. Read as of 2026-09-13.

## LinkedIn serves one page under several addresses

LinkedIn states a page's canonical address in its markup (`<link rel="canonical">`). A company
that renames keeps its old address alive, and both addresses open the same page.

| case | reference | ours | what the page says |
|---|---|---|---|
| PT Alamtri Resources Indonesia (alamtri.com) | `/company/adaroenergy` | `/company/alamtriresourcesindonesia` | `adaroenergy` is the page of "PT Adaro Energy Indonesia Tbk" stating adaro.com; the company renamed to Alamtri and its current page states alamtri.com. The reference's industry (Mining) is the old page's label. |
| Nicholson + Co (nicandco.nz) | `/company/nicandco` | none | `/company/nicandco` now resolves to `/company/hawkbrookpm`, titled "Hawkbrook Project Management", with no stated website. We do not assert a page whose name and site no longer match the company. |
| The Clashify (theclashify.com) | `/company/theclashify` | `/company/the-clashify` | Two live pages, both titled The Clashify, both stating theclashify.com. |
| Appoquinimink School District | `/company/appoquinimink` | `/company/appoquinimink-school-district` | Two live pages, both stating apposchooldistrict.com. The industry label differs between them (the reference's page says Primary and Secondary Education, ours says E-learning). |

## Pages the search does not reach

| case | reference | ours | note |
|---|---|---|---|
| Universidad Central (CO) | `/school/universidad-central-colombia-` | `/company/continua-universidad-central` | A `/school/` page under a slug with a trailing hyphen; our search returns the university's continuing-education unit. Headcount follows the page. |
| Cincinnati College Preparatory Academy | `/school/ccpalionpride` | `/company/cincinnati-college-preparatory` | The school's page lives under an unrelated slug; the company page we hold states ccpaonline.com, a former domain. Industry and headcount follow the page. |
| Astor Hotel Motel (astorhotelalbury.com.au) | `/company/astor-hotel-motel` | none | The hotel's site links its owner's page (`/company/pub-funds-investments`); the hotel's own page does not appear in any search we run. |

## Values that are not LinkedIn's

| case | field | reference | ours | note |
|---|---|---|---|---|
| Shoe Station Group (shoecarnival.com) | headcount band | 3001-10000 | 5001-10000 | 3001-10000 is not a LinkedIn band. The page states 5,001-10,000. |
| September Group LLC | hq city | Cheyenne | Salt Lake City | Cheyenne is the registered-agent address of the Wyoming entity. The company's page states Salt Lake City. |
