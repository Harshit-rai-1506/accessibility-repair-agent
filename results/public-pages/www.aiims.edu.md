# https://www.aiims.edu/

Checked 2026-10-28 with mistralai:mistral-small-2603, agent with tools and guardrails (`scripts/public_pages.py`).

- **Violation score: 37 -> 11** on the saved snapshot (the live page scored 35 with its stylesheets and images).
- Model calls: 28, about 2,323,002 tokens.
- Guardrails: input screen found nothing; rejected attempts: broken_markup 1.

| Rule | What happened | Attempts |
|---|---|---|
| image-alt | fixed | 2 |
| label | fixed | 3 |
| select-name | fixed | 1 |
| color-contrast-enhanced | fixed | 2 |
| frame-title | fixed | 1 |
| link-name | not fixed | 3 |
| target-size | not fixed | 7 |
| heading-order | fixed | 1 |
| region | not fixed | 3 |

For a person to check before using the fix:

- adds text only screen readers hear, written without seeing the page: alt="Twitter"; alt="YouTube"; alt="Union Minister Jagat Prakash Nadda"; alt="Minister of State Jadhav Prataprao Ganpatrao"; alt="Minister of State Anupriya Patel"; alt="Director Dr. Nikhil Tandon"; screen-reader-only text "background", "bottom", "color", "footer", "frame", "header" and 9 more ...
- changes colours: "#b54321" -> "#9a391c", "#bfbfbf". Check the design still works.

Still failing: link-name, target-size, region.

The first 40 lines of the change:

```diff
--- before
+++ after
@@ -37,3 +37,3 @@
 #bd a {
-	color: #b54321;
+	color: #9a391c;
 }
@@ -42,3 +42,3 @@
 	color: #ffffff;
-	background-color: #b54321;
+	background-color: #9a391c;
 }
@@ -1371,5 +1371,5 @@
 <ul>
-<li class="intranet"><a href="https://inhouse.aiims.edu">Intranet Access</a></li>
-<li class="webmail"><a id="webmail" href="http://gsuite.aiims.edu/" target="_blank">@gsuite.aiims.edu</a></li>
-<li class="webmail"><a href="#zt-header">Skip to main</a></li>
+<li class="intranet"><a href="https://inhouse.aiims.edu" style="display: inline-block; padding: 8px 16px; font-size: 16px;">Intranet Access</a></li>
+<li class="webmail"><a id="webmail" href="http://gsuite.aiims.edu/" target="_blank" style="display: inline-block; padding: 12px 24px; font-size: 16px;">@gsuite.aiims.edu</a></li>
+<li class="webmail"><a href="#zt-header" style="padding: 12px 24px;">Skip to main</a></li>
 </ul>
@@ -1419,6 +1419,6 @@
 	<ul class="topmenu">
-<li><a href="/index.php/en/screen-reader-access">Screen Reader Access</a></li>
-<li><a href="/index.php/hi">हिन्दी</a></li>
-<li><a href="http://www.twitter.com/aiims_newdelhi" target="_blank" on-click="return out()"><img src="/images/twitter.png" border="0" width="28"></a></li>
-<li><a href="https://www.youtube.com/channel/UCT0-V_Z5-MuwN5fpbO-25Ag" target="_blank" on-click="return out()"><img src="/images/youtube.png" border="0" width="28"></a></li>
+<li><a href="/index.php/en/screen-reader-access" style="padding: 12px 24px;">Screen Reader Access</a></li>
+<li><a href="/index.php/hi" style="display: inline-block; padding: 8px 16px; font-size: 16px;">हिन्दी</a></li>
+<li><a href="http://www.twitter.com/aiims_newdelhi" target="_blank" on-click="return out()"><img src="/images/twitter.png" border="0" width="28" alt="Twitter"></a></li>
+<li><a href="https://www.youtube.com/channel/UCT0-V_Z5-MuwN5fpbO-25Ag" target="_blank" on-click="return out()"><img src="/images/youtube.png" border="0" width="28" alt="YouTube"></a></li>
 </ul></div>
@@ -1588,12 +1588,12 @@
 	<div class="helpline">
-<p style="margin-top: 0; margin-bottom: 0;"><span style="color: #993300; font-family: 'times new roman', times; font-size: small;"><span class="black_text"><strong><a href="https://ors.gov.in/index.html">OPD Appointment</a></strong></span></span></p>
+<p style="margin-top: 0; margin-bottom: 0;"><span style="color: #993300; font-family: 'times new roman', times; font-size: small;"><span class="black_text"><strong><a href="https://ors.gov.in/index.html" style="display: inline-block; padding: 8px 16px; font-size: 16px;">OPD Appointment</a></strong></span></span></p>
 </div>
 <div class="helpline">
-<p style="margin-top: 0; margin-bottom: 0;"><span style="color: #993300; font-family: 'times new roman', times; font-size: small;"><span class="black_text" style="color: #800080;"><span style="color: #cc0000;"><a href="https://info.aiims.edu/">AIIMS Dashboard</a>&nbsp;<br></span></span></span></p>
+<p style="margin-top: 0; margin-bottom: 0;"><span style="color: #993300; font-family: 'times new roman', times; font-size: small;"><span class="black_text" style="color: #800080;"><span style="color: #cc0000;"><a href="https://info.aiims.edu/" style="display: inline-block; padding: 8px 16px; font-size: 16px;">AIIMS Dashboard</a>&nbsp;<br></span></span></span></p>
... 218 more diff lines
```
