-- Strip every image from the document before pandoc's writer runs.
--
-- Rendering `![alt](url)` to a binary format (docx) makes pandoc fetch <url>
-- SERVER-SIDE to embed it. With user-controlled resume markdown that is an
-- SSRF/LFI primitive: `![x](http://169.254.169.254/...)` or `![x](/etc/passwd)`
-- causes the API to fetch cloud-metadata / internal / local resources during
-- the download render. ATS resumes are text-only, so we drop every image and
-- keep only its alt text.
--
-- This runs on the AST *before* the writer, so no fetch can occur, and it
-- catches ALL image forms uniformly — inline `![](url)`, reference-style
-- `![][ref]`, collapsed/shortcut — because pandoc has already resolved them to
-- Image nodes by the time this filter sees them (a regex on the markdown source
-- misses reference-style; see markdown_linter._IMAGE_RE).
--
-- Why a filter and not `--sandbox`: `--sandbox` is pandoc's native way to run
-- untrusted input, but it breaks docx output on the Debian-packaged pandoc used
-- in the runtime image (that build reads its docx data files from disk, which
-- the sandbox blocks: "Could not find data file .../docx/[Content_Types].xml").
-- The filter is build-independent and works on every pandoc >= 2.

function Image (img)
  -- Replace the image with its caption (alt text). Empty caption -> the image
  -- is dropped entirely. Either way the writer never sees an Image node.
  return img.caption
end
