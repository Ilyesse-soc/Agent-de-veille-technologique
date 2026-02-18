"""Définition des sources officielles/reconnues pour la veille.

Contrainte: seules ces sources sont utilisées.
"""

from __future__ import annotations

from dataclasses import dataclass


CATEGORIES = [
    "Cybersécurité",
    "Big Data",
    "Intelligence Artificielle",
    "Cloud / DevOps",
]


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    category: str


SOURCES: list[Source] = [
    # 🔐 Cybersécurité
    Source("ANSSI", "https://www.ssi.gouv.fr/feed/", "Cybersécurité"),
    Source("CERT-FR", "https://www.cert.ssi.gouv.fr/feed/", "Cybersécurité"),
    Source("CISA", "https://www.cisa.gov/news.xml", "Cybersécurité"),
    Source("The Hacker News", "https://thehackernews.com/rss.xml", "Cybersécurité"),
    Source("Krebs on Security", "https://krebsonsecurity.com/feed/", "Cybersécurité"),

    # 📊 Big Data
    Source("Databricks Blog", "https://www.databricks.com/blog/feed", "Big Data"),
    Source("AWS Big Data Blog", "https://aws.amazon.com/blogs/big-data/feed/", "Big Data"),
    Source(
        "Google Cloud Data Analytics",
        "https://cloud.google.com/blog/topics/data-analytics/rss",
        "Big Data",
    ),

    # 🤖 Intelligence Artificielle / LLM
    Source("arXiv cs.AI", "https://arxiv.org/rss/cs.AI", "Intelligence Artificielle"),
    Source("arXiv cs.LG", "https://arxiv.org/rss/cs.LG", "Intelligence Artificielle"),
    Source("OpenAI Blog", "https://openai.com/blog/rss.xml", "Intelligence Artificielle"),
    Source("Google AI", "https://blog.google/technology/ai/rss/", "Intelligence Artificielle"),
    Source("Papers With Code", "https://paperswithcode.com/rss", "Intelligence Artificielle"),

    # ☁️ Cloud / DevOps
    Source("AWS News Blog", "https://aws.amazon.com/blogs/aws/feed/", "Cloud / DevOps"),
    Source("Kubernetes", "https://kubernetes.io/feed.xml", "Cloud / DevOps"),
    Source("Martin Fowler", "https://martinfowler.com/feed.atom", "Cloud / DevOps"),
    Source("CNCF", "https://www.cncf.io/feed/", "Cloud / DevOps"),
]
