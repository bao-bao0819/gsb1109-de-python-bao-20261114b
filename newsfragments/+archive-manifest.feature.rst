Added ``h11.PageCollector`` and ``h11.ManifestBuilder``: they assemble the
archive interface's paged responses (batch and declared range arrive as
trailers on ``EndOfMessage``) into a publishable ``h11.ArchiveManifest``.
Coverage is only acknowledged when batch numbers agree and the declared
ranges chain without holes; truncated responses cannot testify for missing
pages, confirmed empty pages close their declared gap, and keys whose
values conflict across overlapping replica pages without a comparable
version are quarantined individually.
