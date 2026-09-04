"""The vocabulary shared by every module. Instagram's raw JSON stops at parse.py;
everything downstream speaks only these two types."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Media:
    kind: Literal["image", "video"]
    url: str
    width: int
    height: int
    duration: float | None
    # Relative to the data dir; None until store.py has written the bytes.
    path: str | None


@dataclass(frozen=True)
class Post:
    shortcode: str
    account: str
    timestamp_utc: datetime
    caption: str
    permalink: str
    media: tuple[Media, ...]
    is_carousel: bool
    like_count: int | None
    comment_count: int | None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["timestamp_utc"] = self.timestamp_utc.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        return cls(
            shortcode=data["shortcode"],
            account=data["account"],
            timestamp_utc=datetime.fromisoformat(data["timestamp_utc"]),
            caption=data["caption"],
            permalink=data["permalink"],
            media=tuple(Media(**m) for m in data["media"]),
            is_carousel=data["is_carousel"],
            like_count=data["like_count"],
            comment_count=data["comment_count"],
        )
