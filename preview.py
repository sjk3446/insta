from secrets import token_urlsafe
from urllib.parse import urlsplit


def photo_url(post):
    if post.typename == "GraphImage":
        url = post.url
    elif post.typename == "GraphSidecar":
        url = next((item.display_url for item in post.get_sidecar_nodes() if item.video_url is None), None)
    else:
        return None
    if url:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if parsed.scheme == "https" and hostname.endswith((".cdninstagram.com", ".fbcdn.net")):
            return url
    return None


class PhotoPreview:
    def __init__(self, loader, posts, username, destination, date_from, date_to, cutoff, checked):
        self.id = token_urlsafe(18)
        self.loader = loader
        self.posts = iter(posts)
        self.username = username
        self.destination = destination
        self.date_from = date_from
        self.date_to = date_to
        self.cutoff = cutoff
        self.checked = checked
        self.shown = set()
        self.exhausted = False

    def page(self):
        photos = []
        error = None
        for _ in range(40):
            try:
                post = next(self.posts, None)
                url = photo_url(post) if post is not None and self.includes(post) else None
            except Exception as failure:
                error = str(failure)
                self.exhausted = True
                break
            if post is None:
                self.exhausted = True
                break
            if url is None:
                continue
            self.shown.add(post.shortcode)
            photos.append({
                "shortcode": post.shortcode, "date": post.date_local.date().isoformat(), "image": url,
                "carousel": post.typename == "GraphSidecar",
            })
            if len(photos) == 18:
                break
        return {"id": self.id, "photos": photos, "more": not self.exhausted, "error": error}

    def includes(self, post):
        if self.date_from is not None and not self.date_from <= post.date_local.date() <= self.date_to:
            return False
        if self.cutoff and post.date_utc.strftime("%Y-%m-%d_%H-%M-%S") < self.cutoff:
            return False
        return post.shortcode not in self.shown and post.shortcode not in self.checked

    def close(self):
        self.loader.close()
