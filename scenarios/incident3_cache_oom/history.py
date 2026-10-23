"""Commit history for catalog-api (incident 3). scenarios/build.py turns this into repo/.

Commit 3 is the culprit: it caches rendered product pages in a plain dict keyed by the full request URL.
Marketing and session query parameters make almost every URL unique, so the cache grows without bound
until the container is OOM-killed.
"""

STORE_PY = '''"""Product data (in-memory stand-in for the catalog database)."""


def get_product(product_id):
    pid = int(product_id)
    return {
        "id": pid,
        "name": f"Product {pid}",
        "price": round(9.99 + (pid % 50) * 1.5, 2),
        "description": "A dependable everyday item. " * 20,
        "reviews": [{"stars": 3 + (pid + i) % 3, "text": f"Review {i} for product {pid}. " * 12} for i in range(12)],
    }
'''

TEMPLATES_V1 = '''"""HTML rendering for product pages."""
import html


def render_product(p):
    return (f"<article><h1>{html.escape(p['name'])}</h1><p class=price>${p['price']}</p>"
            f"<p>{html.escape(p['description'])}</p></article>")
'''

TEMPLATES_V2 = '''"""HTML rendering for product pages."""
import html


def render_reviews(reviews):
    return "".join(f"<li>{'*' * r['stars']} {html.escape(r['text'])}</li>" for r in reviews)


def render_product(p):
    return (f"<article><h1>{html.escape(p['name'])}</h1><p class=price>${p['price']}</p>"
            f"<p>{html.escape(p['description'])}</p><ul class=reviews>{render_reviews(p['reviews'])}</ul></article>")
'''

CATALOG_V1 = '''"""Product page rendering."""
from app.store import get_product
from app.templates import render_product


def product_page(product_id, url):
    return render_product(get_product(product_id))
'''

CATALOG_V2 = '''"""Product page rendering."""
import logging

from app.store import get_product
from app.templates import render_product

log = logging.getLogger("catalog")

# Rendering with reviews is slow; keep rendered pages around.
_PAGE_CACHE = {}
log.info("page cache enabled")


def product_page(product_id, url):
    page = _PAGE_CACHE.get(url)
    if page is None:
        page = render_product(get_product(product_id))
        _PAGE_CACHE[url] = page
    return page


def cache_size():
    return len(_PAGE_CACHE)
'''

MAIN_PY = '''"""FastAPI wiring for catalog-api."""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.catalog import product_page

app = FastAPI(title="catalog-api")


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product(product_id: int, request: Request):
    return product_page(product_id, str(request.url))


@app.get("/health")
def health():
    return {"ok": True}
'''

DEPLOY_YAML = '''# Container settings for catalog-api
replicas: 1
resources:
  limits:
    memory: 512Mi
    cpu: "1"
workers: 4
'''

TEST_PY = '''from app.catalog import product_page


def test_product_page_renders():
    page = product_page(7, "/products/7")
    assert "Product 7" in page and "<ul class=reviews>" in page
'''

README_V1 = "# catalog-api\n\nServes product pages for the storefront.\n"
README_V2 = README_V1 + "\n## Campaigns\n\nMarketing emails link to product pages with utm_* parameters.\n"

COMMITS = [
    ("2026-10-10T09:00:00+00:00", "Lena Ortiz <lena@example.com>", "Initial catalog-api service", {
        "app/__init__.py": "", "app/store.py": STORE_PY, "app/templates.py": TEMPLATES_V1, "app/catalog.py": CATALOG_V1,
        "app/main.py": MAIN_PY, "deploy.yaml": DEPLOY_YAML, "tests/test_catalog.py": TEST_PY, "README.md": README_V1,
    }),
    ("2026-10-20T14:10:00+00:00", "Sam Okafor <sam@example.com>", "Add reviews widget to product pages", {
        "app/templates.py": TEMPLATES_V2,
    }),
    ("2026-10-22T09:12:00+00:00", "Sam Okafor <sam@example.com>", "Cache rendered product pages to cut p95", {
        "app/catalog.py": CATALOG_V2,
    }),
    ("2026-10-22T09:20:00+00:00", "Lena Ortiz <lena@example.com>", "Document campaign links", {
        "README.md": README_V2,
    }),
]

CULPRIT_INDEX = 2
