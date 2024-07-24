import json

XPATH_LANGUAGES = (
    "/html/body/div[1]/div/div/footer/div/div/section[1]/div/dl/div/dd/ul/li"
)


def languages(root_start_page):
    """Get all languages from the startpage 'https://www.trustpilot.com/'
    Is using xpath dependend on the layout of the page.
    Might fail, if layout is changed
    """
    lang_dict = {}
    li_lang_elements = root_start_page.xpath(XPATH_LANGUAGES)
    for element in li_lang_elements:
        button = element.xpath("button")[0]
        lang_id = button.attrib.get("lang").lower()
        lang_label = element.xpath("button/span[2]")[0].text_content()
        lang_dict[lang_id] = lang_label
    return lang_dict


XPATH_JSONLD = "/html/head/script[@type='application/ld+json']"
XPATH_STRUCTURED_CONTENT = "//*[@id='__NEXT_DATA__']"

def jsonld(tree):
    json_ld_elements = tree.xpath(XPATH_JSONLD)
    json_ld_contents = []
    for json_ld_element in json_ld_elements:
        json_ld_content_raw = json_ld_element.text_content()
        json_ld_content = json.loads(json_ld_content_raw)
        json_ld_contents.append(json_ld_content)
    return json_ld_contents


def structured_content_data(tree):
    json_elements = tree.xpath(XPATH_STRUCTURED_CONTENT)
    json_content = None
    if json_elements:
        assert len(json_elements) == 1
        json_element = json_elements[0]
        json_content_raw = json_element.text_content()
        json_content = json.loads(json_content_raw)
    return json_content

