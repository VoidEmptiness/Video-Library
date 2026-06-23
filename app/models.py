from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class VideoTag(Base):
    __tablename__ = "video_tags"
    __table_args__ = (UniqueConstraint("video_id", "tag_id", name="uq_video_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)


class FolderTag(Base):
    __tablename__ = "folder_tags"
    __table_args__ = (UniqueConstraint("folder_id", "tag_id", name="uq_folder_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    tags: Mapped[list[Tag]] = relationship(
        "Tag",
        secondary="video_tags",
        back_populates="videos",
        order_by="Tag.name",
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    videos: Mapped[list[Video]] = relationship(
        "Video",
        secondary="video_tags",
        back_populates="tags",
    )
    folders: Mapped[list[Folder]] = relationship(
        "Folder",
        secondary="folder_tags",
        back_populates="tags",
    )


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    match_all: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    tags: Mapped[list[Tag]] = relationship(
        "Tag",
        secondary="folder_tags",
        back_populates="folders",
        order_by="Tag.name",
    )

    parent: Mapped[Folder | None] = relationship(
        "Folder",
        remote_side="Folder.id",
        back_populates="children",
    )

    children: Mapped[list[Folder]] = relationship(
        "Folder",
        back_populates="parent",
    )