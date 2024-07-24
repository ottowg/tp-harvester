import re
import json
import time
import datetime
import tarfile
import io
import random
import argparse
import logging
import queue
from collections import defaultdict

from glob import glob
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

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
        self.path_page_map_infos = Path(path_page_map_infos)
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
        ) = _
        self._persist_page_map_infos()

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
            for idx, sitemap_url in enumerate(sitemap_urls):
                resp = self._get_response(sitemap_url)
                tree = lxml.etree.fromstring(
                    resp.text.encode(), base_url=self.url_sitemap_base
                )
                company_urls_sub = loader.extract_company_urls(tree)
                company_urls.extend(company_urls_sub)
            lang_info.append(
                dict(
                    lang_id=lang_id,
                    lang=lang,
                    n_companies=len(company_urls),
                )
            )
            lang_company_urls[lang_id] = company_urls
        self.logger.info(
            f"Company page urls for {len(lang_info)} languages loaded."
        )
        return lang_info, lang_company_urls


    def load_reviews_by_lang(
        self, language_id, limit=None, max_pages_by_company=None
    ):
        if self.language_overview is None:
            raise Exception("Please load language infos using setup() first")
        urls = self.language_company_urls[language_id]
        random.shuffle(urls)
        url_queue = queue.Queue(maxsize=0)
        if limit is not None:
            urls = urls[:limit]
        urls_to_load = len(urls)
        for url_info in urls:
            url_info["page"] = 1
            url_queue.put(url_info)
        urls_seen = set()
        n_sub_pages = 0
        n_pages_finished = 0
        params = dict(sort="recency")
        while not url_queue.empty():
            url_info = url_queue.get()
            url = url_info["url"]
            page = url_info["page"]
            last_mod = url_info["last_mod"]
            response, next_page = self.get_page(url, params, page)
            # @todo: insert date check here
            if next_page is not None:
                if max_pages_by_company is not None and next_page > max_pages_by_company:
                    next_page = None
                    self.logger.warning(
                        f"For {url} only {page} pages are tested. Maybe there are more."
                    )
            if next_page is not None:
                url_queue.put(dict(url=url, page=next_page, last_mod=last_mod))
            if response is not None:
                page_data_info = self._scrape_structured_infos(response)
                page_data_info |= dict(url_base=url, page=page, url_request=url)
                page_data_info["url_base_last_mod"] = url_info["last_mod"]
                company_key = url.split("/review/")[1]
                page_data_info["company_key"] = company_key
                urls_seen.add(url)
                n_sub_pages += 1
                url_queue.task_done()
                stats = dict(
                        pages_started=len(urls_seen),
                        pages_finished=n_pages_finished,
                        pages_total=urls_to_load,
                        sub_pages_loaded=n_sub_pages,
                        language_id=language_id,
                        current_url=url,
                        current_page=page,
                        len_queue=url_queue.qsize(),
                )
                yield page_data_info, stats
            if response is None or next_page is None:
                n_pages_finished += 1


    def get_page(self, url, params, page):
        next_page = page + 1
        params |= dict(page=page)
        if (
            page == 1
        ):  # if a parameter page=1 exits, the request is forwarded to the page without any paramter. Other parameters like "sort=recency" are not handled anymore.
            del params["page"]
        resp = None
        url = f"{url}?{urlencode(params)}"
        try:
            resp = self._get_response(url)
            if not are_effective_similar_urls(url,resp.url):
                self.logger.info(
                    f"URL: {url} was redirected to {resp.url}. Not handled. sitmap_info_wrong?"
                )
                resp, next_page = None, None

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

    def _scrape_structured_infos(self, response):
        structured_infos = dict(
            url_response=response.url,
            date=utc_timestamp(),
            headers=dict(response.headers),
        )
        tree = lxml.html.fromstring(response.text)
        structured_infos["jsonld"] = scraper.jsonld(tree)
        structured_infos["structured_page_content"] = scraper.structured_content_data(tree)
        return structured_infos

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
            total_urls = len(self.language_company_urls[language_id])
            for json_ld_info, stats in json_lds_iter:
                page = json_ld_info["page"]
                company_key = json_ld_info["company_key"]
                filename = f"{company_key}/{page}.json"
                _add_data_to_tar(tar, json_ld_info, filename)
                end_persist = time.time()
                total_time = end_persist - start_total
                time_per_page = total_time / stats["sub_pages_loaded"]
                print(
                        f"\rcompanies: ({stats['pages_finished']}, {stats['pages_started']}, {stats['pages_total']}) | n_pages: {stats['sub_pages_loaded']} current_page_nr: {stats['current_page']} | time_total: {total_time:.0f} | one_page: {time_per_page:5.1f} | {company_key} ",
                    end="",
                )
        print()

    def load_page_map_infos(self):
        tar_filename = self._get_last_page_map_info_tar_gz()
        if tar_filename is None:
            self.logger.info(
                "No language and url data found. load data with .setup() (takes ~10 minutes)."
            )
            return
        print(tar_filename)
        today = datetime.datetime.today().strftime("%Y-%m-%d")
        tar_file_date = tar_filename.name[:-7]
        self.logger.info(
            f"Load language and url data from {tar_file_date} ..."
        )
        if today > tar_file_date:
            self.logger.info(
                f"language and url data might be outdated (Loaded on {tar_file_date}. Reload with .setup()"
            )
        with tarfile.open(tar_filename, "r:gz") as tar:
            self.available_languages = _read_data_from_tar(tar, "available_languages.json")
            self.language_overview = _read_data_from_tar(tar, "language_overview.json")
            self.language_company_urls = _read_data_from_tar(tar, "language_company_urls.json")
        self.logger.info(
            f"Load language and url data for {len(self.language_overview)} languages."
        )

    def _persist_page_map_infos(self):
        self.logger.info("Persist language and url infos.")
        path = self.path_page_map_infos
        sitemap_date = datetime.datetime.today().strftime("%Y-%m-%d")
        path.mkdir(parents=True, exist_ok=True)
        tar_filename = path / f"{sitemap_date}.tar.gz"
        with tarfile.open(tar_filename, "w:gz") as tar:
            filename = "available_languages.json"
            _add_data_to_tar(tar, self.available_languages, filename)
            filename = "language_overview.json"
            _add_data_to_tar(tar, self.language_overview, filename)
            filename = "language_company_urls.json"
            _add_data_to_tar(tar, self.language_company_urls, filename)

    def _get_last_page_map_info_tar_gz(self):
        path = self.path_page_map_infos
        sub_folder = glob(f"{str(path)}/*.tar.gz")
        year_pattern = re.compile(
            r"^[0-9]{4,4}-[0-9]{2,2}-[0-9]{2,2}$"
        )
        tar_files = [Path(fn) for fn in sub_folder]
        tar_files = [fn for fn in tar_files
                     if year_pattern.match(fn.name[:-7])]
        tar_files = [fn for fn in tar_files if not fn.is_dir()]
        if not tar_files:
            return
        tar_files.sort(key=lambda x: x.stem)
        last_tar_file = tar_files[-1]
        return last_tar_file


def utc_timestamp():
    return datetime.datetime.fromtimestamp(time.time()).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _read_data_from_tar(tar, filename):
    file = tar.extractfile(filename)
    content_binary = file.read()
    content = content_binary.decode('utf-8')
    data = json.loads(content)
    return data

def _add_data_to_tar(tar, data, filename): 
    data_raw = json.dumps(data)
    data_raw_encoded = data_raw.encode("utf-8")
    fileobj = io.BytesIO(data_raw_encoded)
    tarinfo = tarfile.TarInfo(name=filename)
    tarinfo.size = len(data_raw_encoded)
    tar.addfile(tarinfo, fileobj)

def are_effective_similar_urls(url, url_compare):
    # Parse the URLs
    parsed_url1 = urlparse(url)
    parsed_url2 = urlparse(url_compare)
            
    # Extract the query parameters and sort them for comparison
    query_params1 = parse_qs(parsed_url1.query)
    query_params2 = parse_qs(parsed_url2.query)

    # Compare the parsed URLs (excluding query parameters) and sorted query parameters
    urls_are_same = (parsed_url1._replace(query="") == parsed_url2._replace(query="")) and (query_params1 == query_params2)

    return urls_are_same


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
            logging.FileHandler(f"{args.data_path}/log_tp_harvester_{today}.log"),  # Log to a file
            # logging.StreamHandler()  # Log to console
        ],
    )
    harvester = TPCollector(args.url, args.mail, args.data_path)
    if harvester.language_overview is None:
        print("Loading company url data first")
        harvester.setup()
    harvester.save_by_language(
        args.data_path, args.language_id, limit=args.limit,
        max_pages_by_company=args.max_pages_by_company, verbose=False
    )

if __name__ == "__main__":
	main()


