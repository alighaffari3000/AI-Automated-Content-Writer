"""What the article tells a search engine about itself.

The point of building this in code is that every value can be traced to
something already checked. These tests hold that line: the catalogue decides
what a product costs, the body decides what the FAQ says, and a claim with no
source behind it never appears at all.
"""

from __future__ import annotations

from app.config import ContentConfig, SeoConfig, SiteConfig
from app.schemas import ArticleDraft
from app.structured_data import build, faq_pairs

SITE = SiteConfig(name="Example Site", public_url="https://example.com")
CONTENT = ContentConfig(language="fa")
CONFIG = SeoConfig(min_faq_entries=2, structured_data=True)

CATALOGUE = [
    {
        "slug": "deye-sun-12k",
        "name": "Deye SUN-12K",
        "description": "A 12 kW hybrid inverter.",
        "price": "1850",
        "currency": "USD",
        "brand": "Deye",
    },
    {"slug": "no-price", "name": "A product with no price"},
]

FAQ_BODY = """## ظرفیت قابل استفاده چیست؟

بخشی از ظرفیت اسمی که می‌توانید بدون آسیب به باتری مصرف کنید.

[[IMAGE: a battery bank | یک بانک باتری]]

## چه تفاوتی با ظرفیت اسمی دارد؟

ظرفیت اسمی عددی است که روی جعبه نوشته شده و [همیشه](/articles/x) در دسترس نیست.

## جمع‌بندی

خرید را بر اساس ظرفیت قابل استفاده انجام دهید.
"""


def draft(**overrides) -> ArticleDraft:
    fields = {
        "title": "ظرفیت قابل استفاده باتری",
        "slug": "usable-capacity",
        "excerpt": "خلاصه‌ای کوتاه.",
        "body": FAQ_BODY,
        "seo_title": "ظرفیت قابل استفاده باتری چیست",
        "meta_description": "چقدر از ظرفیت اسمی واقعا در اختیار شماست.",
    }
    fields.update(overrides)
    return ArticleDraft(**fields)


def blocks(**kwargs) -> list[dict]:
    return build(
        kwargs.pop("draft", None) or draft(),
        body=kwargs.pop("body", FAQ_BODY),
        site=SITE,
        content=CONTENT,
        config=kwargs.pop("config", CONFIG),
        products=kwargs.pop("products", CATALOGUE),
        keywords=kwargs.pop("keywords", ["ظرفیت قابل استفاده"]),
        featured_image=kwargs.pop("featured_image", "/uploads/lead.png"),
    )


def types_of(found: list[dict]) -> list[str]:
    return [block["@type"] for block in found]


def one(found: list[dict], type_name: str) -> dict:
    return next(block for block in found if block["@type"] == type_name)


def test_an_article_is_always_described():
    article = one(blocks(), "Article")
    assert article["headline"] == "ظرفیت قابل استفاده باتری چیست"
    assert article["inLanguage"] == "fa"
    assert article["publisher"]["name"] == "Example Site"
    assert article["mainEntityOfPage"]["@id"] == "https://example.com/usable-capacity"
    assert article["image"] == ["/uploads/lead.png"]


def test_no_publication_date_is_invented():
    """A human decides when this publishes, so only the site knows the date."""
    assert "datePublished" not in one(blocks(), "Article")


def test_question_headings_become_the_faq():
    faq = one(blocks(), "FAQPage")
    questions = [entry["name"] for entry in faq["mainEntity"]]
    assert questions == ["ظرفیت قابل استفاده چیست؟", "چه تفاوتی با ظرفیت اسمی دارد؟"]


def test_the_answer_is_the_prose_under_the_heading_only():
    answer = faq_pairs(FAQ_BODY)[0][1]
    assert answer == "بخشی از ظرفیت اسمی که می‌توانید بدون آسیب به باتری مصرف کنید."


def test_link_and_image_syntax_is_stripped_out_of_an_answer():
    answer = faq_pairs(FAQ_BODY)[1][1]
    assert "](" not in answer and "[[IMAGE" not in answer
    assert "همیشه" in answer


def test_one_question_is_not_an_faq():
    body = "## آیا این یک سوال است؟\n\nبله.\n\n## یک بخش عادی\n\nمتن.\n"  # noqa: RUF001
    assert "FAQPage" not in types_of(blocks(body=body))


def test_a_product_is_described_from_the_catalogue_not_the_article():
    found = blocks(draft=draft(related_products=["deye-sun-12k"]))
    product = one(found, "Product")
    assert product["name"] == "Deye SUN-12K"
    assert product["brand"]["name"] == "Deye"
    assert product["offers"]["price"] == "1850"
    assert product["url"] == "https://example.com/products/deye-sun-12k"


def test_a_product_the_catalogue_does_not_have_is_not_described():
    found = blocks(draft=draft(related_products=["something-invented"]))
    assert "Product" not in types_of(found)


def test_a_price_without_a_currency_is_left_out_rather_than_guessed():
    found = blocks(draft=draft(related_products=["no-price"]))
    assert "offers" not in one(found, "Product")


def test_structured_data_can_be_turned_off_entirely():
    assert blocks(config=SeoConfig(structured_data=False)) == []
