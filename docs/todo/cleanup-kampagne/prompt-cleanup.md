Bitte helfe mir beim Cleanup der Codebase.

Ich habe eine code-review per KI gemacht, gespeichert unter reports/code-review-full-ox-alpha-2026-08-24.md sowie eine Analyse mit SonarQube, gespeichert unter reports/sonar-review-full-2408.json
Wenn du weitere Infos über einzelne Sonar-Findings benötigst, kannst du diese mit ```curl -s -u "${SONAR_TOKEN}:" "http://localhost:9000/api/issues/search?componentKeys=trading-platform-local&resolved=false&ps=500" | python3 -m json.tool``` abrufen (Token aus Umgebungsvariable, nie in commits/issues kopieren), wenn du weitere Infos zu Filtern, bspw. nach Severity brauchst, schaue mit ```ctx7``` cli nach docs zu sonarqube (token nie in commits/issues kopieren)
Allein von SonarQube gibt es schon 1190 Issues insgesamt, viele davon doppeln sich aber (es gibt bspw. 225x Regel S3776 und 254x S8415/S8409)

Bitte gehe wie folgt vor:
Als Erstes triagen wir die Sonar-Findings:
1. Ich möchte, dass du als erstes das aktuelle commit als "cleanup-baseline" taggst.
2. Danach möchte ich, dass wir reports/sonar-review-full-2408.json nach Rule gruppieren.
3. Dann entscheiden wir pro Regel, wie wir fortfahren. Erstelle dazu in reports/cleanup-rule-decisions-draft.md eine Tabelle, in der wir pro Regel folgendes notieren: rule; count; typische Message, betroffene Gebiete; Vorschlag (fixen/auslassen)
4. auf mein OK warten, dann ein gh issue über die sonar findings anlegen, das mit "cleanup" taggen

Danach die Findings aus Ox Code-Review:
5. Aus reports/code-review-full-ox-alpha-2026-08-24.md alle critical und Major findings extrahieren, und nach Gebiet clustern (`live_runner`, `runner` (+ engines), `tests`, `frontend` (+ viewer), `data_tools`/`validation`, `misc-backend`)
6. gegen Sonar dedupen (bereits abgedeckte Bugs/Vulns/Blocker-Issues referencen, nicht doppelt ticketen)
7. auf mein OK warten, dann pro Gebiet ein Github-Issue anlegen, mit folgendem: komplette Findings-Liste (File+Line, Severity, Description), Acceptance Criteria, offene Needs decision-Fragen
alle Issues sollen mit "cleanup" getagged werden, Issues die offene decisions haben zusätzlich mit "needs decision" taggen


Du selbst schreibst keinen Code, sondern hilfst lediglich beim Erstellen von Issues.
