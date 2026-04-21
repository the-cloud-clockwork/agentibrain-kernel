# Identity — Who You Are

The root node of the vault. Every AI agent reads `identity/` first for context. Fill these in before anything else.

## Files

| File | Purpose |
|------|---------|
| `about-me.md` | Core identity — who you are, how you work, how you communicate |
| `goals.md` | What you're pursuing — this week, month, quarter, year |
| `principles.md` | Decision rules — how you decide |
| `stack.md` | Tools, expertise, infrastructure you depend on |

## How to start

Each file ships as `<name>.template.md`. Copy it to `<name>.md` and fill in your content. The AI agents read the `.md` versions only.

```bash
cd identity/
for f in about-me goals principles stack; do
  cp "${f}.template.md" "${f}.md"
done
```

Then edit `about-me.md` first — that's the file everything else builds on.
