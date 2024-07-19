import time
from functools import wraps

from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_result


def get_function_get_response(
    session, retry_attempts, retry_wait, timeout, max_calls_per_minute, logger
):
    retry_decorator = retry(
        stop=stop_after_attempt(retry_attempts),
        wait=wait_fixed(retry_wait),
        retry=retry_if_result(lambda x: x == 500),
    )

    @retry_decorator
    @requests_per_minute(max_calls_per_minute, logger)
    def get_response(url):
        resp = session.get(url, timeout=timeout)
        if resp.status_code == 500:
            logger.warning(
                f"Server error (500) encountered at {url}, retrying..."
            )
            return 500
        resp.raise_for_status()  # Raise an HTTPError for bad responses
        return resp

    return get_response


def requests_per_minute(max_calls_per_minute, logger):
    calls = []

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal calls
            now = time.time()
            # Remove calls that are older than 60 seconds
            calls = [call for call in calls if now - call < 60]
            if (
                max_calls_per_minute is not None
                and len(calls) >= max_calls_per_minute
            ):
                # Calculate the time to wait until the oldest call is older than 60 seconds
                wait_time = 60 - (now - calls[0])
                logger.debug(
                    f"Wait {wait_time:0.2f} seconds. (Max requests per minute: {max_calls_per_minute})"
                )
                time.sleep(wait_time)
                # After waiting, clean up old calls again and proceed
                now = time.time()
            calls = [call for call in calls if now - call < 60]
            calls.append(now)
            return func(*args, **kwargs)

        return wrapper

    return decorator


NAME_SPACES = {"sms": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def extract_sitemap_urls(tree):
    """Extract all sitemap sub pages for a given sitemap xml tree"""
    sitemap_elements = tree.xpath("sms:sitemap/sms:loc", namespaces=NAME_SPACES)
    sitemap_urls = [e.text for e in sitemap_elements]
    return sitemap_urls


def extract_company_urls(tree, slag_pattern="/review/"):
    """extract all company urls from a sitemap url
    Extraction is based on sitmap xml format.
    """
    url_elements = tree.xpath("sms:url", namespaces=NAME_SPACES)
    url_infos = []
    for url_element in url_elements:
        url = url_element.xpath("sms:loc", namespaces=NAME_SPACES)[0].text
        last_mod_elements = url_element.xpath(
            "sms:lastmod", namespaces=NAME_SPACES
        )
        last_mod = None
        if last_mod_elements:
            last_mod = last_mod_elements[0].text
        if slag_pattern in url:
            url_infos.append(dict(url=url, last_mod=last_mod))
    return url_infos
