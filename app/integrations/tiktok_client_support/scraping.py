from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from playwright.sync_api import Error, Page

from app.models import ScrapedComment


class TikTokScrapingMixin:
    def _collect_from_response(self, response: Any, collected: dict[str, ScrapedComment]) -> None:
        url = response.url.lower()
        if "comment" not in url:
            return

        content_type = (response.headers or {}).get("content-type", "").lower()
        if "json" not in content_type and "text/plain" not in content_type:
            return

        try:
            payload = response.json()
        except (Error, json.JSONDecodeError):
            return

        for comment in self._extract_comments_from_payload(payload):
            self._upsert_scraped_comment(collected, comment)

    def _extract_comments_from_payload(self, payload: Any) -> list[ScrapedComment]:
        results: dict[str, ScrapedComment] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    normalized_key = key.lower()
                    if normalized_key in {"comments", "comment_list", "commentlist"} and isinstance(value, list):
                        for item in value:
                            comment = self._normalize_comment_payload(item)
                            if comment:
                                results.setdefault(comment.comment_id, comment)
                    walk(value)
                return

            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return list(results.values())

    def _normalize_comment_payload(self, payload: Any) -> ScrapedComment | None:
        if not isinstance(payload, dict):
            return None

        comment_text = str(payload.get("text") or payload.get("comment") or "").strip()
        if not comment_text:
            return None

        user_payload = payload.get("user") or payload.get("user_info") or payload.get("author") or {}
        username = (
            self._first_non_empty(
                user_payload.get("unique_id"),
                user_payload.get("uniqueId"),
                user_payload.get("username"),
                payload.get("unique_id"),
            )
            or "unknown"
        )
        display_name = (
            self._first_non_empty(
                user_payload.get("nickname"),
                user_payload.get("display_name"),
                user_payload.get("displayName"),
                username,
            )
            or username
        )

        comment_id = str(
            self._first_non_empty(
                payload.get("cid"),
                payload.get("id"),
                payload.get("comment_id"),
                f"{username}:{comment_text}",
            )
        )

        likes_raw = self._first_non_empty(
            payload.get("digg_count"),
            payload.get("like_count"),
            payload.get("likes"),
        )
        likes = int(likes_raw) if likes_raw not in (None, "") else None

        published_at = self._parse_timestamp(
            self._first_non_empty(payload.get("create_time"), payload.get("created_at"))
        )
        reply_author_usernames = self._extract_reply_usernames_from_payload(payload)
        return ScrapedComment(
            video_url=self._active_video_url,
            comment_id=comment_id,
            author_username=str(username),
            author_display_name=str(display_name),
            text=comment_text,
            likes=likes,
            published_at=published_at,
            reply_author_usernames=reply_author_usernames,
        )

    def _extract_comments_from_dom(self, page: Page) -> list[ScrapedComment]:
        rows = page.evaluate(
            """
            () => {
              const rowSelectors = [
                '[data-e2e="comment-level-1"]',
                '[data-e2e="comment-item"]',
                'div[class*="CommentItem"]',
                'li[class*="CommentItem"]'
              ];

              const elements = [];
              const seen = new Set();
              for (const selector of rowSelectors) {
                for (const element of document.querySelectorAll(selector)) {
                  if (!seen.has(element)) {
                    seen.add(element);
                    elements.push(element);
                  }
                }
              }

              const normalizeLine = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              const looksLikeMeta = (value) => {
                return (
                  /^\\d+$/.test(value) ||
                  /^\\d+[smhdwy]$/.test(value.toLowerCase()) ||
                  ['reply', 'like', 'liked', 'pinned'].includes(value.toLowerCase())
                );
              };

              return elements.map((element, index) => {
                const anchor = element.querySelector('a[href*="/@"]');
                const href = anchor ? (anchor.getAttribute('href') || '') : '';
                const usernameMatch = href.match(new RegExp('/@([^/?]+)'));
                const username = usernameMatch
                  ? usernameMatch[1]
                  : normalizeLine(anchor ? anchor.textContent : '').replace(/^@/, '') || 'unknown';
                const displayName = normalizeLine(anchor ? anchor.textContent : '') || username;
                const textNode =
                  element.querySelector('[data-e2e*="comment-text"]') ||
                  element.querySelector('p') ||
                  element.querySelector('span[data-text="true"]');
                const lines = Array.from(
                  new Set(
                    (element.innerText || element.textContent || '')
                      .split(/\\n+/)
                      .map(normalizeLine)
                      .filter(Boolean)
                  )
                );
                const text =
                  normalizeLine(textNode ? textNode.textContent : '') ||
                  lines.find((line) => {
                    return (
                      line !== displayName &&
                      line !== username &&
                      line !== `@${username}` &&
                      !looksLikeMeta(line)
                    );
                  }) ||
                  '';
                const likeNode = Array.from(element.querySelectorAll('span, strong')).find((node) => {
                  const value = normalizeLine(node.textContent);
                  return /^\\d+$/.test(value);
                });
                const likes = likeNode ? normalizeLine(likeNode.textContent) : '';
                const timeNode = element.querySelector('time') || element.querySelector('[data-e2e*="comment-time"]');
                const publishedAt = normalizeLine(
                  timeNode ? (timeNode.getAttribute('datetime') || timeNode.textContent) : ''
                );
                const anchorUsernames = Array.from(element.querySelectorAll('a[href*="/@"]'))
                  .map((node) => {
                    const hrefValue = node.getAttribute('href') || '';
                    const match = hrefValue.match(new RegExp('/@([^/?]+)'));
                    return match ? match[1] : normalizeLine(node.textContent).replace(/^@/, '');
                  })
                  .filter(Boolean);
                const replyAuthors = Array.from(new Set(anchorUsernames.filter((value) => value !== username)));

                return {
                  comment_id: element.getAttribute('data-comment-id') || `${username}:${index}:${text}`,
                  author_username: username,
                  author_display_name: displayName,
                  text,
                  likes,
                  published_at: publishedAt,
                  reply_author_usernames: replyAuthors
                };
              }).filter(item => item.text);
            }
            """
        )

        comments_by_id: dict[str, ScrapedComment] = {}
        for row in rows:
            likes = int(row["likes"]) if str(row.get("likes") or "").isdigit() else None
            comment = ScrapedComment(
                video_url=self._active_video_url,
                comment_id=str(row["comment_id"]),
                author_username=str(row["author_username"]),
                author_display_name=str(row["author_display_name"]),
                text=str(row["text"]),
                likes=likes,
                published_at=str(row.get("published_at") or "") or None,
                reply_author_usernames=tuple(
                    str(item) for item in row.get("reply_author_usernames") or []
                ),
            )
            comments_by_id.setdefault(comment.comment_id, comment)
        return list(comments_by_id.values())

    def _collect_comments_from_dom(self, page: Page, collected: dict[str, ScrapedComment]) -> int:
        before_count = len(collected)
        for comment in self._extract_comments_from_dom(page):
            self._upsert_scraped_comment(collected, comment)
        return len(collected) - before_count

    def _upsert_scraped_comment(
        self,
        collected: dict[str, ScrapedComment],
        comment: ScrapedComment,
    ) -> None:
        existing = collected.get(comment.comment_id)
        if existing is not None:
            collected[comment.comment_id] = self._merge_scraped_comments(existing, comment)
            return

        matched_id = self._find_matching_comment_id(collected, comment)
        if matched_id is None:
            collected[comment.comment_id] = comment
            return

        merged = self._merge_scraped_comments(collected[matched_id], comment)
        keep_incoming_id = (
            not self._is_synthetic_comment_id(comment.comment_id)
            and self._is_synthetic_comment_id(matched_id)
        )
        if keep_incoming_id:
            del collected[matched_id]
            collected[comment.comment_id] = replace(merged, comment_id=comment.comment_id)
            return

        collected[matched_id] = replace(merged, comment_id=matched_id)

    def _find_matching_comment_id(
        self,
        collected: dict[str, ScrapedComment],
        comment: ScrapedComment,
    ) -> str | None:
        incoming_signature = self._build_comment_signature(comment)
        if incoming_signature is None:
            return None

        incoming_is_synthetic = self._is_synthetic_comment_id(comment.comment_id)
        for existing_id, existing_comment in collected.items():
            existing_signature = self._build_comment_signature(existing_comment)
            if existing_signature != incoming_signature:
                continue

            existing_is_synthetic = self._is_synthetic_comment_id(existing_id)
            if incoming_is_synthetic or existing_is_synthetic:
                return existing_id

        return None

    def _merge_scraped_comments(self, left: ScrapedComment, right: ScrapedComment) -> ScrapedComment:
        reply_authors = tuple(sorted(set(left.reply_author_usernames) | set(right.reply_author_usernames)))
        eligible_accounts = tuple(sorted(set(left.eligible_account_names) | set(right.eligible_account_names)))
        likes = left.likes if left.likes is not None else right.likes
        published_at = left.published_at or right.published_at
        author_username = left.author_username if left.author_username != "unknown" else right.author_username
        author_display_name = (
            left.author_display_name
            if left.author_display_name and left.author_display_name != "unknown"
            else right.author_display_name
        )
        text = left.text if left.text.strip() else right.text
        return replace(
            left,
            author_username=author_username,
            author_display_name=author_display_name,
            text=text,
            likes=likes,
            published_at=published_at,
            reply_author_usernames=reply_authors,
            eligible_account_names=eligible_accounts,
        )

    def _build_comment_signature(self, comment: ScrapedComment) -> tuple[str, str] | None:
        author_username = self._normalize_username(comment.author_username)
        normalized_text = re.sub(r"\s+", " ", comment.text or "").strip().casefold()
        if not author_username or not normalized_text:
            return None
        return author_username, normalized_text

    @staticmethod
    def _is_synthetic_comment_id(comment_id: str) -> bool:
        return not str(comment_id or "").strip().isdigit()
