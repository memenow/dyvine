"""Shared pagination constants for bulk content download loops.

Both ``services/users.py::UserService._process_download`` and
``services/posts.py::PostService.download_all_user_posts`` cap their
outer pagination loops with the same heuristic::

    max_pages = (total // PAGE_SIZE) * PAGE_MULTIPLIER + PAGE_SLACK

When the upstream total is unknown the loop falls back to
``MAX_PAGES_FALLBACK``. The fallback covers ``MAX_PAGES_FALLBACK *
PAGE_SIZE`` items per loop -- 50_000 for the user download loop
(``PAGE_SIZE=100`` in ``services/users.py``) and 10_000 for the post
bulk loop (``PAGE_SIZE=20`` in ``services/posts.py``). Accounts above
those numbers will hit the cap and the calling service will record a
partial completion via the existing status branches.

``PAGE_SIZE`` itself stays at each call site because every fetcher
accepts a different page size; only the slack / multiplier / fallback
are shared so a future tuning PR can adjust the cap budget in one
place.
"""

# Extra pages on top of the expected ``total // PAGE_SIZE`` so the loop
# tolerates upstream cursor jitter without truncating legitimate runs.
PAGE_SLACK = 20

# Multiplier applied to the expected page count so a moderately sticky
# upstream still has room to make progress before the cap fires.
PAGE_MULTIPLIER = 2

# Used when the upstream does not expose a total (likes-only runs and
# the bulk-post loop without a profile total). Bounds the loop at
# ``MAX_PAGES_FALLBACK * PAGE_SIZE`` items per call.
MAX_PAGES_FALLBACK = 500
