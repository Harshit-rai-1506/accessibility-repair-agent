# https://www.iitb.ac.in/

Checked 2026-10-28 with mistralai:mistral-small-2603, agent with tools and guardrails (`scripts/public_pages.py`).

- **Violation score: 24 -> 9** on the saved snapshot (the live page scored 23 with its stylesheets and images).
- Model calls: 22, about 326,255 tokens.
- Guardrails: input screen found nothing; rejected attempts: none.

| Rule | What happened | Attempts |
|---|---|---|
| aria-allowed-attr | fixed | 2 |
| aria-required-children | not fixed | 3 |
| link-name | fixed | 1 |
| target-size | not fixed | 4 |
| landmark-unique | fixed | 4 |
| page-has-heading-one | fixed | 1 |

For a person to check before using the fix:

- adds visible text: "Indian Institute of Technology Bombay" (its words appear elsewhere in the page).
- adds text only screen readers hear, written without seeing the page: aria-label="Top navigation"; aria-label="Secondary navigation"; aria-label="Main site navigation"; aria-label="Home"; aria-label="Secondary content"; aria-label="Featured content sidebar" ...

Still failing: aria-required-children, target-size.

The first 40 lines of the change:

```diff
--- before
+++ after
@@ -142,3 +142,3 @@
     <header id="header" class="header" role="banner" aria-label="Site header">
-                  <nav class="navbar" id="navbar-top">
+                  <nav class="navbar" id="navbar-top" aria-label="Top navigation">
         <div class="container">
@@ -149,3 +149,3 @@
       <div class="content">
-      <div class="tb-megamenu tb-megamenu-secondarynavigation" role="navigation" aria-label="Main navigation" data-once="tb-megamenu">
+      <div class="tb-megamenu tb-megamenu-secondarynavigation" role="navigation" aria-label="Secondary navigation" data-once="tb-megamenu">
   <div class="container">
@@ -154,3 +154,3 @@
             <li class="tb-megamenu-item level-1 mega mega-align-center dropdown" data-id="menu_link_content:5a670a80-8e35-494f-a52d-ec7c99df2f16" data-level="1" data-type="menu_item" data-class="" data-xicon="" data-caption="" data-alignsub="center" data-group="0" data-hidewcol="0" data-hidesub="0" data-label="" aria-level="1">
-      <span class="dropdown-toggle tb-megamenu-no-link" aria-label="Open main submenu" aria-expanded="false" tabindex="0">
+      <span class="dropdown-toggle tb-megamenu-no-link" role="button" aria-label="Open main submenu" aria-expanded="false" tabindex="0">
           Students
@@ -164,3 +164,3 @@
         <li class="tb-megamenu-item level-2 mega" data-id="menu_link_content:9adb5525-53df-4fc0-9b70-5dc1a58da9bb" data-level="2" data-type="menu_item" data-class="" data-xicon="" data-caption="" data-alignsub="" data-group="0" data-hidewcol="0" data-hidesub="0" data-label="" aria-level="2">
-      <a href="https://www.iitb.ac.in/sites/default/files/2025-11/campusCodeOfConduct.pdf" title="Code of Conduct">
+      <a href="https://www.iitb.ac.in/sites/default/files/2025-11/campusCodeOfConduct.pdf" title="Code of Conduct" style="display: inline-block; padding: 8px 16px; min-width: 24px; min-height: 24px; line-height: 24px;">
           Code of Conduct
@@ -171,3 +171,3 @@
         <li class="tb-megamenu-item level-2 mega" data-id="menu_link_content:ee81763e-0336-4f9d-bb41-b62a4ea20866" data-level="2" data-type="menu_item" data-class="" data-xicon="" data-caption="" data-alignsub="" data-group="0" data-hidewcol="0" data-hidesub="0" data-label="" aria-level="2">
-      <a href="https://gymkhana.iitb.ac.in/">
+      <a href="https://gymkhana.iitb.ac.in/" style="display: inline-block; padding: 8px 16px; min-width: 24px; min-height: 24px; line-height: 24px;">
           Gymkhana
@@ -178,3 +178,3 @@
         <li class="tb-megamenu-item level-2 mega" data-id="menu_link_content:ee81763e-0336-4f9d-bb41-b62a4ea20866" data-level="2" data-type="menu_item" data-class="" data-xicon="" data-caption="" data-alignsub="" data-group="0" data-hidewcol="0" data-hidesub="0" data-label="" aria-level="2">
-      <a href="https://gymkhana.iitb.ac.in/">
+      <a href="https://gymkhana.iitb.ac.in/" style="display: inline-block; padding: 8px 16px; min-width: 24px; min-height: 24px; line-height: 24px;">
           Gymkhana
@@ -185,3 +185,3 @@
         <li class="tb-megamenu-item level-2 mega" data-id="menu_link_content:f2e336fc-f7bb-42f2-b7e5-0fa6d0c96a77" data-level="2" data-type="menu_item" data-class="" data-xicon="" data-caption="" data-alignsub="" data-group="0" data-hidewcol="0" data-hidesub="0" data-label="" aria-level="2">
-      <a href="https://campus.placements.iitb.ac.in/">
+      <a href="https://campus.placements.iitb.ac.in/" style="display: inline-block; padding: 8px 16px; min-width: 24px; min-height: 24px; line-height: 24px;">
           Placements
@@ -192,3 +192,3 @@
         <li class="tb-megamenu-item level-2 mega" data-id="menu_link_content:da0b897a-3500-4bd9-8603-6bf02d3a9582" data-level="2" data-type="menu_item" data-class="" data-xicon="" data-caption="" data-alignsub="" data-group="0" data-hidewcol="0" data-hidesub="0" data-label="" aria-level="2">
-      <a href="https://dosa.iitb.ac.in">
... 191 more diff lines
```
