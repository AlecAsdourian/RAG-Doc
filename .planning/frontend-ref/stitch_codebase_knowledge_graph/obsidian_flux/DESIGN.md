---
name: Obsidian Flux
colors:
  surface: '#13131b'
  surface-dim: '#13131b'
  surface-bright: '#393841'
  surface-container-lowest: '#0d0d15'
  surface-container-low: '#1b1b23'
  surface-container: '#1f1f27'
  surface-container-high: '#292932'
  surface-container-highest: '#34343d'
  on-surface: '#e4e1ed'
  on-surface-variant: '#c7c4d7'
  inverse-surface: '#e4e1ed'
  inverse-on-surface: '#303038'
  outline: '#908fa0'
  outline-variant: '#464554'
  surface-tint: '#c0c1ff'
  primary: '#c0c1ff'
  on-primary: '#1000a9'
  primary-container: '#8083ff'
  on-primary-container: '#0d0096'
  inverse-primary: '#494bd6'
  secondary: '#4edea3'
  on-secondary: '#003824'
  secondary-container: '#00a572'
  on-secondary-container: '#00311f'
  tertiary: '#ffb783'
  on-tertiary: '#4f2500'
  tertiary-container: '#d97721'
  on-tertiary-container: '#452000'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#e1e0ff'
  primary-fixed-dim: '#c0c1ff'
  on-primary-fixed: '#07006c'
  on-primary-fixed-variant: '#2f2ebe'
  secondary-fixed: '#6ffbbe'
  secondary-fixed-dim: '#4edea3'
  on-secondary-fixed: '#002113'
  on-secondary-fixed-variant: '#005236'
  tertiary-fixed: '#ffdcc5'
  tertiary-fixed-dim: '#ffb783'
  on-tertiary-fixed: '#301400'
  on-tertiary-fixed-variant: '#703700'
  background: '#13131b'
  on-background: '#e4e1ed'
  surface-variant: '#34343d'
typography:
  h1:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '600'
    lineHeight: '1.2'
    letterSpacing: -0.02em
  h2:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: '1.3'
    letterSpacing: -0.01em
  body-main:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: '1.6'
    letterSpacing: '0'
  body-mono:
    fontFamily: Fira Code
    fontSize: 13px
    fontWeight: '450'
    lineHeight: '1.5'
    letterSpacing: '0'
  label-caps:
    fontFamily: Space Grotesk
    fontSize: 11px
    fontWeight: '700'
    lineHeight: '1'
    letterSpacing: 0.08em
  code-sm:
    fontFamily: Fira Code
    fontSize: 12px
    fontWeight: '400'
    lineHeight: '1.4'
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  unit: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 48px
  panel-gap: 1px
  sidebar-width: 260px
---

## Brand & Style

This design system is engineered for deep technical immersion, prioritizing high-density information display without cognitive overload. The aesthetic sits at the intersection of **Minimalism** and **Glassmorphism**, utilizing a "Developer Dark Mode" philosophy that mimics high-end IDE environments. 

The brand personality is authoritative, precise, and performant. It evokes a sense of "intelligence under the hood," suitable for advanced RAG (Retrieval-Augmented Generation) workflows. Visual interest is achieved through rhythmic spacing and surgical use of vibrant accents rather than decorative flourishes. Surfaces use subtle translucency to maintain context during complex navigation, while high-contrast typography ensures zero-latency readability of technical data and model outputs.

## Colors

The palette is anchored in a multi-layered dark grey scale. The foundation is **Obsidian (#0B0E14)** for the deepest background layers (workspace canvas), stepping up to **Charcoal (#151921)** for modular panels and UI chrome.

- **Electric Indigo (#6366F1)**: Reserved for primary actions, focus states, and the representation of "intelligence" or AI-driven processes.
- **Emerald (#10B981)**: Utilized strictly for success states, active system health, and verified data retrievals.
- **Subtle Borders (#1F2937)**: All structural separation is handled by this low-contrast grey to prevent visual noise while maintaining clear containment.
- **Text**: Headlines and primary data use **#F9FAFB** for maximum legibility against dark backgrounds, while metadata uses **#9CA3AF**.

## Typography

This design system utilizes a dual-font strategy to distinguish between UI orchestration and technical content. 

1.  **Inter**: The workhorse for the interface. It is used for all navigation, settings, and standard body text. Weights are kept strictly to 400 and 600 to maintain a clean, systematic look.
2.  **Fira Code**: Applied to any data that is "machine-generated" or "retrieved." This includes code snippets, JSON outputs, document chunks, and node IDs in the graph visualization.
3.  **Space Grotesk**: Used sparingly for high-level technical labels and "eyebrow" text in uppercase to add a subtle futuristic, geometric edge to the headers.

## Layout & Spacing

The layout follows a **Fixed Grid** modular panel approach. Elements are contained within "tiles" separated by 1px gaps (the border color) to create a sophisticated, dashboard-like density typical of IDEs.

- **The Workspace**: A multi-pane layout with a fixed left navigation (collapsed or 260px), a flexible central graph/editor area, and an optional right-hand inspector panel.
- **Modular Panels**: Use a 16px (md) internal padding for content, but 8px (sm) for dense lists or navigation items.
- **Rhythm**: All margins and paddings must be multiples of 4px. Use "Dense" scaling for list items (32px height) to maximize vertical information density.

## Elevation & Depth

In this design system, depth is not conveyed through shadows, but through **Tonal Layering** and **Glassmorphism**.

- **Level 0 (Canvas)**: Obsidian (#0B0E14). The base layer where graph nodes live.
- **Level 1 (Panels)**: Charcoal (#151921). Solid background for sidebars and main content areas.
- **Level 2 (Overlays/Modals)**: Frosted Glass. Use a background blur of `20px` and a fill of `rgba(21, 25, 33, 0.8)`. These must have a 1px border of `rgba(255, 255, 255, 0.1)`.
- **Interactive States**: Hovering over a list item or card should transition the background to a slightly lighter `rgba(255, 255, 255, 0.03)` rather than raising it with a shadow.

## Shapes

The shape language is "Soft" yet disciplined. A base radius of `4px` (0.25rem) is applied to buttons, inputs, and panel corners. This creates a modern feel without the playfulness of fully rounded UI.

- **Graph Nodes**: Should use a `6px` radius to slightly differentiate them from standard UI components.
- **Code Blocks**: Always use a `4px` radius. 
- **Buttons**: Match the `4px` standard. Do not use pill shapes; maintain the architectural, blocky aesthetic.

## Components

- **Buttons**: Primary buttons use a solid Electric Indigo background with white text. Secondary buttons use a ghost style (transparent background, #1F2937 border).
- **Interactive Nodes**: Graph nodes feature an Obsidian background, an Electric Indigo 1px border, and a small Fira Code label. Active nodes should have an outer glow (box-shadow) using a faint Indigo tint.
- **Input Fields**: Dark backgrounds (#0B0E14) with a subtle border. On focus, the border changes to Electric Indigo with no glow.
- **Chips/Badges**: Small, rectangular with 2px radius. Use Charcoal background with Indigo or Emerald text for status indicators.
- **Scrollbars**: Custom-styled to be ultra-thin (4px), using #1F2937 as the thumb color to remain unobtrusive until hovered.
- **Modular Panels**: Feature a header bar with a background-color of `rgba(255,255,255,0.02)` and a bottom border to separate control actions from content.