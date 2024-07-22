# tp-harvester
Scrape jsonld review infos from TP.

## Obsevations:
 * TP has company related review pages (i.e. "../reviews/<company_name>").
   * The review pages incl. 20 Reviews
   * The review pages of one company can be iterated with the `page` parameter in the URL
     * Example: "../reviews/<company_name>&page=3
   * The reviews are present as structured data in the html pages (i.e. the content in the <script type="application/ld+json"> Tag in the header.)
 * All company pages are listed in the sitemap pages (used to support for search indexer (e.g. google))
   * Example: Sitemap: https://sitemaps.<tp_url>/index_en-us.xml
   * Lists for all available languages exist
   * Each company review page URL info incl. the date of the last modification
 * Langages (incl. language_ids) can be crawled from the start page

## Idea
 * To get all reviews for one language:
   * get all company review page URLs
   * for each company review page URL
     * crawl structured reviews from start page
     * try next page, until the next page does not exist: 404 Error
     * Persist all reviews
   * use `sort=recency` parameter to not load reviews multiple times.

## Limitations
 * robots.txt does not allow to scrape (especially `*sort=recency`)
 * page limit
   * Sending to many requests per minute:
     * only 9 pages per company review page could be loaded
   * general blocking of the harvester is not seen yet.
 * sometimes Error 500 occurs (no rule found yet)

## Implementation
 * The harvester loads initially all available company review urls for all languages from the pagemaps when started from command line
   * this takes around 5 minutes
   * The urls are loaded on disk (currently unpacked, ~45MB)
   * the pagemap data is stored by default here: `sitemap_infos` (i.e. relative path)
 * A request per minute limitation is used. (60 requests per minute. Set hard coded [here](https://github.dev/ottowg/tp-harvester/blob/main/tp_harvester.md))
 * Friendly crawling is default. Please add mail adress and institutional url.
 * Testing:
   * To test the functionality you can limit the number of different company review pages and the number of sub pages to load for each company review page
     * `limit`, `max_pages_by_company`

### Persistance
 * The reviews are loaded in a tar.gz file for each language.
 * It is not tested how big the file will be for each language.

## Example usage to load portuguise data
 * python tp_harvester.py "your/data/path" pt-pt "your mail address" "your url" --limit 10 --max_pages_by_company 2

## Example language overview (2024-07-18)

| lang_id   | lang           |   n_companies |
|:----------|:---------------|--------------:|
| da-dk     | Danmark        |        108284 |
| de-at     | Österreich     |        108548 |
| de-ch     | Schweiz        |        108548 |
| de-de     | Deutschland    |        108548 |
| en-au     | Australia      |        645734 |
| en-ca     | Canada         |        645734 |
| en-gb     | United Kingdom |        648212 |
| en-ie     | Ireland        |        645734 |
| en-nz     | New Zealand    |        645734 |
| en-us     | United States  |        648490 |
| es-es     | España         |         66227 |
| fi-fi     | Suomi          |          6434 |
| fr-be     | Belgique       |        100932 |
| fr-fr     | France         |        100932 |
| it-it     | Italia         |         74351 |
| ja-jp     | 日本           |          3924 |
| nb-no     | Norge          |          6815 |
| nl-nl     | Nederland      |         81676 |
| pl-pl     | Polska         |         20904 |
| pt-br     | Brasil         |         26682 |
| pt-pt     | Portugal       |         26682 |
| sv-se     | Sverige        |         34593 |


## Installation
 * clone the repo
 * install python >=3.9
 * install requirements from `requirements.txt`

## Test run
 * language_id de-de
 * 1000 company page urls (randomly chosen)
 * 5767 sub pages
   * 60.478 Reviews
 * ~2h runtime
 * => 115340 Reviews from 1000 companies
 * Not all possible sub pages for each company are loaded (limitations above (403 errors))
 * `python tp_harvester.py /data_ssds/disk01/ottowg/trustpilot de-de <mail> <harvester_url> --limit 1000`

## Known Errors
 * [ ] Not fixed 403 issue
 * [ ] URLs could be company pages without review info: 
   * e.g. <https://de.trustpilot.com/review/www.puregym.com/location?sort=recency>
   * Solution: better filter for sitemapt URLs


## Optimizations:
 * Better handle locations  (e.g. <https://de.trustpilot.com/review/www.imocarwash.com/location/wiesbaden>)
   * Each company can have multiple location. Reviews can be location specific.
