# Kapazitätsübersicht für OpenShift-Cluster

### Hinweis zur Entstehung
Dieses Skript wurde in einem iterativen KI-Dialog entwickelt. Anforderungen, Auswahl der Metriken, Layout-Entscheidungen und Tests kamen vom Anwender. Code-Generierung, Parser-Logik und HTML/CSS-Templating wurden von der KI umgesetzt. 

### Überblick
Python-Skript, das eine HTML-Kapazitätsübersicht eines OpenShift-Clusters erzeugt.
Läuft auf dem Bastion-Host, braucht oc (eingeloggt) und Python 3.
Keine weiteren Abhängigkeiten notwendig. Die Kapazitätsübersicht ist eine Momentaufnahme.

### Nutzung
```bash
python3 quick-cap-script.py > capacity-te-$(date +%F).html              #z.B. Test-Cluster
```

### Ausgabe
Eine einzelne HTML-Datei mit inline CSS.

## Ablauf
### Schritt 1: Daten holen
Vier oc-Aufrufe, alles was das Skript braucht:
```bash
oc get nodes -o json
oc get pods -A -o json
oc get pv -o json
oc adm top nodes
```


### Schritt 2: Parsen
Drei Parser-Funktionen wandeln Ressourcenwerte des Clusters in lesbare Zahlen um:
```bash
pc (v)                                  # CPU-Werte > Cores
pm_gb(v)                                # Speicher > GB
pm_mb(v)                                # Wrapper, ruft pm_gb() auf und multipliziert x 1024
parse_top_line(line)    # parst eine Zeile von oc adm top nodes --no-headers
```

### Schritt 3: Daten aggregieren
Drei Aggregationen laufen über die Pod-Liste:
- **Requests pro Node** - das Skript iteriert über alle laufenden Pods, liest spec.Nodename und summiert die resources.requersts.cpu/memory aller Container auf dem jeweiligen Node.
Ergebnis: zwei Dictionaries **node_req_cpu** und **node_req_mem** verknüpft mit dem Node-Name.
- **Usage pro Node** - Kommt direkt aus oc adm top nodes. Wird in node_usage_cpu und node_usage_mem gespeichert.
- **Requests/Limits pro Node** - Gleiche Pod-Schleife aber gruppiert nach metadata.namespace.

### Schritt 4: Node-Rows zusammenbauen
Pro Node werden die drei Datenquellen zusammengeführt und notwendige Berechnungen für die Darstellung durchgeführt.

### Schritt 5: Cluster-Summen berechnen
Einfache Summen über alle Nodes inkl. Prozent-Berechnung.

### Schritt 6: Namespace Ranking
 - **Top 10 CPU Req**: sortiert nach CPU-Requests absteigend
     Balken-Referenz: Verhältnis zwischen reserviert und gesamt verfügbar (Req/Alloc)
 - **Top 10 RAM Req**: sortiert nach RAM-Requests absteigend
     Balken-Referenz: Verhältnis zwischen reserviert und gesamt verfügbar (Req/Alloc)
 - **Top 10 CPU Hamster**: sortiert nach CPU-Differenz relativ zum größten Diff-Wert
     Balken-Referenz: Verhältnis zwischen reserviert und tatsächlich verbraucht
 - **Top 10 RAM Hamster**: sortiert nach RAM-Differenz relativ zum größten Diff-Wert
     Balken-Referenz: Verhältnis zwischen reserviert und tatsächlich verbraucht
