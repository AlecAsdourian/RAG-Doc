# Phase 13: Web UI - Search & Chat - Context

**Gathered:** 2026-02-01
**Status:** Ready for planning

<vision>
## How This Should Work

A side-by-side panel interface where developers can search and chat simultaneously. Search results appear on one side, chat on the other — both always visible.

When you search, you get cards with code previews that expand on click — compact by default but detailed when you need it. The chat responds with accurate answers that cite their sources: inline [1], [2] references, a collapsible "Sources" section, and auto-linked code mentions that connect back to search results.

The experience starts with a clean public landing page. Login (via GitHub/GitLab OAuth) unlocks the search/chat interface. Once authenticated, you're straight into the tool.

</vision>

<essential>
## What Must Be Nailed

- **Fast, relevant search results** — The search needs to surface the right code/docs instantly
- **Quality chat responses** — The LLM answers need to be accurate and cite sources
- **Clean, developer-friendly UX** — It needs to feel like a tool developers actually want to use

All three are equally important — can't compromise on any.

</essential>

<boundaries>
## What's Out of Scope

- Mobile optimization — desktop-first, mobile can wait
- Advanced theming/customization — one good design
- Complex documentation browsing hierarchies — focus on search/chat

</boundaries>

<specifics>
## Specific Ideas

**Look & Feel (PRIORITY):**
- **Minimal** — ruthlessly simple, no visual clutter, only what's needed
- **Professional** — polished, confident design that feels production-ready
- **Clean transitions** — smooth, subtle animations (150-300ms), no jarring state changes
- Inspired by Linear/Vercel — lots of whitespace, subtle colors, elegant typography

**Tech Stack:**
- React + Next.js

**Search Results:**
- Cards with code preview that expand on click
- Show file path and line numbers
- Ranked by relevance

**Chat Behavior:**
- Inline citations [1], [2] linking to source code
- Collapsible "Sources" section below answers
- Auto-linked code mentions become clickable

**Auth Flow:**
- Public landing page for unauthenticated users
- Login required to access search/chat
- GitHub/GitLab OAuth (already implemented via Supabase)

</specifics>

<notes>
## Additional Context

The RAG backend (Phases 11-12) is complete with:
- Hybrid search (vector + FTS)
- LLM answer generation with semantic caching
- Full retrieval pipeline

This UI is the user-facing layer on top of that infrastructure.

</notes>

---

*Phase: 13-web-ui-search-chat*
*Context gathered: 2026-02-01*
