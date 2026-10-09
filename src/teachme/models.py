from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class SourceReference:
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: str


@dataclass
class NewsItem:
    title: str
    url: str
    published_at: str
    summary: str
    sources: list[SourceReference] = field(default_factory=list)

    def as_prompt_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "published_at": self.published_at,
            "summary": self.summary,
            "sources": [
                {
                    "publication": reference.source_name,
                    "title": reference.title,
                    "url": reference.url,
                    "published_at": reference.published_at,
                }
                for reference in self.sources
            ],
        }