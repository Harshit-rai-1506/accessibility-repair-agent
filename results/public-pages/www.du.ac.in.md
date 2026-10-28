# https://www.du.ac.in/

Checked 2026-10-28 with mistralai:mistral-small-2603, agent with tools and guardrails (`scripts/public_pages.py`).

- **Violation score: 54 -> 29** on the saved snapshot (the live page scored 61 with its stylesheets and images).
- Model calls: 50, about 334,648 tokens.
- Guardrails: input screen found nothing; rejected attempts: broken_markup 3.

| Rule | What happened | Attempts |
|---|---|---|
| aria-allowed-attr | fixed | 1 |
| aria-required-children | not fixed | 3 |
| aria-required-parent | not fixed | 4 |
| image-alt | fixed | 3 |
| color-contrast | not fixed | 3 |
| link-name | not fixed | 4 |
| list | not fixed | 3 |
| listitem | not fixed | 4 |
| marquee | fixed | 2 |
| heading-order | not fixed | 4 |
| landmark-one-main | fixed | 1 |
| landmark-unique | fixed | 1 |
| region | fixed by earlier fix | 0 |
| empty-heading | fixed | 1 |

For a person to check before using the fix:

- adds visible text: "Gallery Highlights" (its words appear elsewhere in the page).
- adds text only screen readers hear, written without seeing the page: aria-label="Main Navigation"; alt="University of Delhi Logo"; aria-label="University of Delhi homepage"; aria-label="Scrolling announcements"; alt="Online Teaching and Learning"; alt="Glimpses of DU" ...
- adds page regions: <main>

Still failing: aria-required-children, aria-required-parent, color-contrast, link-name, list, listitem, heading-order.

The first 40 lines of the change:

```diff
--- before
+++ after
@@ -68,3 +68,3 @@
 .fb_iframe_widget{display:inline-block;position:relative}.fb_iframe_widget span{display:inline-block;position:relative;text-align:justify}.fb_iframe_widget iframe{position:absolute}.fb_iframe_widget_fluid_desktop,.fb_iframe_widget_fluid_desktop span,.fb_iframe_widget_fluid_desktop iframe{max-width:100%}.fb_iframe_widget_fluid_desktop iframe{min-width:220px;position:relative}.fb_iframe_widget_lift{z-index:1}.fb_iframe_widget_fluid{display:inline}.fb_iframe_widget_fluid span{width:100%}
-</style><script charset="utf-8" src="https://platform.twitter.com/js/tweet.7a6003258de838c35e66fd0f50ebdb0f.js"></script></head><body class="" data-aos-easing="ease" data-aos-duration="400" data-aos-delay="0">
+</style><script charset="utf-8" src="https://platform.twitter.com/js/tweet.7a6003258de838c35e66fd0f50ebdb0f.js"></script></head><body class="" data-aos-easing="ease" data-aos-duration="400" data-aos-delay="0"><main>
 <!-- Google Tag Manager (noscript) -->
@@ -84,3 +84,3 @@
     <div class="container-fluid">
-    <nav class="navbar navbar-expand-lg navbar-light bg-light oie" style="padding: 20px; justify-content: center;">
+    <nav class="navbar navbar-expand-lg navbar-light bg-light oie" style="padding: 20px; justify-content: center;" aria-label="Main Navigation">
     <a href="http://ioe.du.ac.in/" target="_blank"><font size="5px">Institution of Eminence</font></a>
@@ -90,2 +90,3 @@
     <div class="collapse navbar-collapse top-right" id="navbarSupportedContent">
+        <ul>
         <li><a href="http://www.du.ac.in/hi/" target="_blank">हिंदी</a></li>
@@ -104,2 +105,3 @@
         <li><a href="index.php">Home&nbsp;<span class="fas fa-home" style="opacity: 1;"></span></a></li>
+        </ul>
     </div>
@@ -112,3 +114,3 @@
         <div class="float-left du-logo-head">
-            <a href="index.php"> <img src="uploads/du/images/logo-du.png" width="300px" class="margin-responsive-logo"> </a>
+            <a href="index.php"> <img src="uploads/du/images/logo-du.png" width="300px" class="margin-responsive-logo" alt="University of Delhi Logo"> </a>
         </div>
@@ -485,3 +487,3 @@
                         
-                        <a href="index.php?page="></a><a href="https://www.ncwwomenhelpline.in/" target="_blank">www.ncwwomenhelpline.in&nbsp;<span class="fa fa-link"></span></a><br>
+                        <a href="index.php?page=" aria-label="University of Delhi homepage"></a><a href="https://www.ncwwomenhelpline.in/" target="_blank">www.ncwwomenhelpline.in&nbsp;<span class="fa fa-link"></span></a><br>
               <br>
@@ -681,7 +683,9 @@
 <li class="nav-item animate slideIn" onclick="window.open('https://www.du.ac.in/index.php?mact=News,cntnt01,detail,0&amp;cntnt01articleid=15520&amp;cntnt01returnid=219')">
-    <div class="nav-link active" data-toggle="pill" aria-selected="true">
-        <div class="media switch-buttons">
-            <div class="mr-4 ml-3 text-center"><span class="day">27</span><br><span class="month">Oct</span></div>
-            <div class="media-body">
-                <h6 class="mt-1 d-inline line-height-inline">Notification regarding PGCF - 2024 Syllabi</h6>
+    <div role="tablist">
+        <div class="nav-link active" data-toggle="pill" role="tab" aria-selected="true" aria-controls="pills-latestnews" aria-labelledby="pills-latestnews-tab">
+            <div class="media switch-buttons">
... 1965 more diff lines
```
