import re
import json
import time
import datetime
import tarfile
import io
import random
import argparse
import logging

from glob import glob
from pathlib import Path
from urllib.parse import urlencode

import requests
import lxml
import lxml.html

from tenacity import RetryError
from tqdm import tqdm
from requests.exceptions import HTTPError

import loader
import scraper


class TPCollector:
    def __init__(self, url, mail, path_page_map_infos="sitemap_infos"):
        self.logger = logging.getLogger("TP-HARVESTER")
        self.url_start_page = "https://www.trustpilot.com/"
        self.url_sitemap_base = "https://sitemaps.trustpilot.com"

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": f"ReviewResearchProject/0.1 ({url}, {mail})",
                "From": mail,
            }
        )
        self.retry_attempts = 3
        self.retry_wait = 1  # second
        self.timeout = 5
        self.max_calls_per_minute = 60

        self._get_response = loader.get_function_get_response(
            self.session,
            self.retry_attempts,
            self.retry_wait,
            self.timeout,
            self.max_calls_per_minute,
            self.logger,
        )
        self.language_overview = None
        self.path_page_map_infos = path_page_map_infos
        self.load_page_map_infos()

    def setup(self):
        """Load Metadata
        * Available languages
        * Company Pages per language
        """
        self.available_languages = self._get_languages()
        _ = self._collect_language_infos()
        (
            self.language_overview,
            self.language_company_urls,
            self.language_category_urls,
        ) = _
        self._persist_page_map_infos()

    def load_reviews_by_lang(
        self, language_id, limit=None, max_pages_by_company=None
    ):
        if self.language_overview is None:
            raise Exception("Please load language infos using setup() first")
        urls = self.language_company_urls[language_id]
        random.shuffle(urls)
        for idx, url_info in enumerate(urls):
            params = dict(sort="recency")
            url = url_info["url"]
            for json_ld_info in self.load_reviews_by_url(
                url, params, max_pages_by_company=max_pages_by_company
            ):
                # @todo: insert date check here
                json_ld_info["url_base_last_mod"] = url_info["last_mod"]
                yield json_ld_info
            if idx + 1 >= limit:
                break

    def load_reviews_by_url(self, url, params, max_pages_by_company):
        for page_idx, response in enumerate(
            self._page_iterator(
                url,
                params,
                start_page=1,
                max_pages_by_company=max_pages_by_company,
            )
        ):
            json_ld_info = self._scrape_json_ld_infos(response)
            json_ld_info |= dict(url_base=url, page=page_idx + 1)
            yield json_ld_info

    def _get_languages(self):
        resp = self._get_response(self.url_start_page)
        root_start_page = lxml.html.fromstring(resp.text)
        languages = scraper.languages(root_start_page)
        self.logger.info(
            f"{len(languages)} Available languages scraped from start page."
        )
        return languages

    def _collect_language_infos(self):
        """Load an overview of available companies for each language."""
        lang_info = []
        lang_company_urls = {}
        lang_category_urls = {}
        languages = tqdm(
            self.available_languages.items(),
            ncols=100,
            desc="Load language.",
            unit=" Languages",
        )
        for lang_id, lang in languages:
            sitemap_url_base = f"{self.url_sitemap_base}/index_{lang_id}.xml"
            resp = self._get_response(sitemap_url_base)
            tree = lxml.etree.fromstring(
                resp.text.encode(), base_url=self.url_sitemap_base
            )
            # Get the sub sitmaps incl. all available page urls
            sitemap_urls = loader.extract_sitemap_urls(tree)
            company_urls = []
            category_urls = []
            for idx, sitemap_url in enumerate(sitemap_urls):
                resp = self._get_response(sitemap_url)
                tree = lxml.etree.fromstring(
                    resp.text.encode(), base_url=self.url_sitemap_base
                )
                company_urls_sub = loader.extract_company_urls(tree)
                category_urls_sub = loader.extract_company_urls(
                    tree, slag_pattern="/categories/"
                )
                company_urls.extend(company_urls_sub)
                category_urls.extend(category_urls_sub)
            lang_info.append(
                dict(
                    lang_id=lang_id,
                    lang=lang,
                    n_companies=len(company_urls),
                    n_categories=len(category_urls),
                )
            )
            lang_company_urls[lang_id] = company_urls
        self.logger.info(
            f"Company page urls for {len(lang_info)} languages loaded."
        )
        return lang_info, lang_company_urls, lang_category_urls

    def _page_iterator(
        self, start_url, params, start_page=1, max_pages_by_company=None
    ):
        """iterate over urls with increasing page attribute ("?page=1", "?page=2", ..)
        if page is not exist: Stop
        """
        page = start_page
        while page is not None:
            if max_pages_by_company is not None and page > max_pages_by_company:
                self.logger.warning(
                    f"For {start_url} only {page} pages are tested. Maybe there are more."
                )
                break
            response, page = self.get_page(start_url, params, page)
            if response is not None:
                yield response

    def get_page(self, url, params, page):
        next_page = page + 1
        params |= dict(page=page)
        if (
            page == 1
        ):  # if a parameter page=1 exits, the request is forwarded to the page without any paramter. Other parameters like "sort=recency" are not handled anymore.
            del params["page"]
        resp = None
        try:
            url = f"{url}?{urlencode(params)}"
            resp = self._get_response(url)
        except HTTPError as e:
            # page with this page number does not exist. break
            if e.response.status_code == 404:
                self.logger.debug(
                    f"URL: {url} has {next_page - 1} pages. Download finished."
                )
                next_page = None
            else:
                self.logger.error(
                    f"Request failed for {url}. Stopped on page {page -1}: {e}"
                )
                next_page = None
        except RetryError as e:
            self.logger.error(
                f"Request failed for {url}. Stopped on page {page -1}: {e}"
            )
            next_page = None
        except Exception as e:
            self.logger.error(f"Request failed for {url}: {e}")
        return resp, next_page

    def _scrape_json_ld_infos(self, response):
        json_ld_info = dict(
            url=response.url,
            date=utc_timestamp(),
            headers=dict(response.headers),
        )
        tree = lxml.html.fromstring(response.text)
        json_ld_info["jsonld"] = scraper.jsonld(tree)
        return json_ld_info

    def save_by_language(
        self,
        base_path,
        language_id,
        limit=None,
        max_pages_by_company=None,
        min_year_mod=None,
        verbose=False,
    ):
        start_date = datetime.datetime.today().strftime("%Y-%m-%d")
        # if min_year_mod is not None:
        #    urls = [u for u in urls if int(u["last_mod"][:4]) >= min_year_mod]
        #    print(f"{len(urls)} companies have data modified after {min_year_mod}")
        base_path = Path(base_path)
        base_path.mkdir(parents=True, exist_ok=True)
        tar_filename = (
            f"{start_date}-{language_id}-trustpilot-reviews-jsonld.tar.gz"
        )
        tar_filename = base_path / tar_filename
        self.logger.info(f"Persistance file name: {tar_filename}")
        with tarfile.open(tar_filename, "w:gz") as tar:
            start_total = time.time()
            json_lds_iter = self.load_reviews_by_lang(
                language_id,
                limit=limit,
                max_pages_by_company=max_pages_by_company,
            )
            total_urls = len(self.language_company_urls["da-dk"])
            last_url = None
            n_urls = 0
            for pages_loaded, json_ld_info in enumerate(json_lds_iter, 1):
                url = json_ld_info["url_base"]
                if url != last_url:
                    n_urls += 1
                last_url = url
                company_key = Path(url).name
                page = json_ld_info["page"]
                filename = f"{company_key}/{page}.json"
                page_info_raw = json.dumps(json_ld_info)
                data = page_info_raw.encode("utf-8")
                fileobj = io.BytesIO(data)
                tarinfo = tarfile.TarInfo(name=filename)
                tarinfo.size = len(data)
                tar.addfile(tarinfo, fileobj)
                end_persist = time.time()
                total_time = end_persist - start_total
                time_per_page = total_time / pages_loaded
                print(
                    f"\rcompanies: {n_urls}/{total_urls} | total_pages: {pages_loaded} | time_total: {total_time:.0f} | one_page: {time_per_page:5.1f} | {company_key} ",
                    end="",
                )
        print()

    def load_page_map_infos(self):
        data_path = self._get_last_page_map_info_path()
        if data_path is None:
            self.logger.info(
                "No language and url data found. load data with .setup() (takes ~6 minutes)."
            )
            return
        today = datetime.datetime.today().strftime("%Y-%m-%d")
        self.logger.info(
            f"Load language and url data from {data_path.name} ..."
        )
        if today > data_path.name:
            self.logger.info(
                f"language and url data might be outdated. Loaded on {data_path.name}. Reload with .setup()"
            )
        with (data_path / "available_languages.json").open("r") as f:
            self.available_languages = json.load(f)
        with (data_path / "language_overview.json").open("r") as f:
            self.language_overview = json.load(f)
        with (data_path / "language_company_urls.json").open("r") as f:
            self.language_company_urls = json.load(f)
        with (data_path / "language_category_urls.json").open("r") as f:
            self.language_category_urls = json.load(f)
        self.logger.info(
            f"Load language and url data for {len(self.language_overview)} languages."
        )

    def _persist_page_map_infos(self):
        self.logger.info("Persist language and url infos.")
        path = self.path_page_map_infos
        sitemap_date = datetime.datetime.today().strftime("%Y-%m-%d")
        path = path / sitemap_date
        path.mkdir(parents=True, exist_ok=True)
        with (path / "available_languages.json").open("w") as f:
            json.dump(self.available_languages, f)
        with (path / "language_overview.json").open("w") as f:
            json.dump(self.language_overview, f)
        with (path / "language_company_urls.json").open("w") as f:
            json.dump(self.language_company_urls, f)
        with (path / "language_category_urls.json").open("w") as f:
            json.dump(self.language_category_urls, f)

    def _get_last_page_map_info_path(self):
        path = self.path_page_map_infos
        sub_folder = glob(f"{str(path)}/*")
        ends_with_year_pattern = re.compile(
            r".*[0-9]{4,4}-[0-9]{2,2}-[0-9]{2,2}$"
        )
        sub_folders = [
            Path(folder)
            for folder in sub_folder
            if ends_with_year_pattern.search(folder)
        ]
        sub_folders = [f for f in sub_folders if f.is_dir()]
        if not sub_folders:
            return
        sub_folders.sort(key=lambda x: x.name)
        last_folder = sub_folders[-1]
        return last_folder


def utc_timestamp():
    return datetime.datetime.fromtimestamp(time.time()).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class Waiter:
    """Depricated"""

    def __init__(self, seconds: int, verbose=False):
        assert isinstance(seconds, int)
        assert seconds > 0
        self.seconds = seconds
        self.verbose = verbose
        self.total = 0

    def wait(self):
        to_wait = self.seconds
        while to_wait != 0:
            if self.verbose:
                print(
                    f"\r ...wait {to_wait} s. (total waited: {self.total} s)",
                    end="",
                )
            time.sleep(1)
            to_wait -= 1
            self.total += 1
            if self.verbose:
                print("\r" + 50 * " ", end="")
                print("\r", end="")
def main():
    parser = argparse.ArgumentParser(description="Process some inputs.")
    
    # Define the obligatory parameters
   
    parser.add_argument("data_path", type=str, help="The path where you want to store the harvested data.")
    parser.add_argument("language_id", type=str, help="language id for harvesting.")
    parser.add_argument("mail", type=str, help="Your email address (friendly crawling).")
    parser.add_argument("url", type=str, help="The URL of your institution (friendly crawling).")
    parser.add_argument("--limit", type=int, help="An optional limit of companies to crawl for the language.", default=None)
    parser.add_argument("--max_pages_by_company", type=int, help="An optional limit of pages to harvest for each company.", default=None)
    
    # Parse the arguments
    args = parser.parse_args()
    
    # Access the parameters
    import logging

    # for handler in logging.root.handlers[:]:
    #    logging.root.removeHandler(handler)
    today = datetime.datetime.today().strftime("%Y-%m-%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(f"tp_harvester_{today}.log"),  # Log to a file
            # logging.StreamHandler()  # Log to console
        ],
    )
    harvester = TPCollector(args.url, args.mail)
    harvester.save_by_language(
        args.data_path, args.language_id, limit=args.limit,
        max_pages_by_company=args.max_pages_by_company, verbose=False
    )

if __name__ == "__main__":
	main()
