from playwright.sync_api import sync_playwright
import csv
import smtplib
from email.message import EmailMessage
import os

def scrape_open_summer_internships():
    URL = "https://app.the-trackr.com/uk-finance/summer-internships"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()

        internships = []
        def handle_resp(response):
            if response.request.resource_type == "xhr" and "internships" in response.url:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        lst = data.get("vacancies") or data.get("internships") or []
                    elif isinstance(data, list):
                        lst = data
                    else:
                        lst = []
                    if isinstance(lst, list):
                        internships.extend(lst)
                except:
                    pass

        page.on("response", handle_resp)
        page.goto(URL, wait_until="networkidle", timeout=60000)

        prev_h = page.evaluate("() => document.body.scrollHeight")
        while True:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)
            new_h = page.evaluate("() => document.body.scrollHeight")
            if new_h == prev_h:
                break
            prev_h = new_h

        browser.close()
    valid = [i for i in internships if isinstance(i, dict)]
    open_offers = []
    for i in valid:
        if i.get("openingDate") is not None:
            company  = i.get("company")
            title = (i.get("name") or "").strip()
            comp = company.get("name")
            category = (i.get("category") or "").strip()
            url = (i.get("url")      or "").strip()
            open_offers.append((comp, title, category, url))

    return open_offers

def read_process_csv(csv_path="processus_ouverts.csv"):
    """
    Lit le fichier CSV et retourne une liste de dicts.
    Chaque dict correspond à une ligne, avec pour clés les en-têtes de colonnes.
    """
    with open(csv_path, mode="r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        processes = [row for row in reader]
    return processes


def ecriture_csv(open_offers, output_file="processus_ouverts.csv"):
    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Company", "Title", "Category", "Url"])
        writer.writerows(open_offers)
    print(f"{len(open_offers)} offres exportées dans : {output_file}")
    return output_file

def send_email(open_offers, old_procs):
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT   = int(os.getenv("SMTP_PORT", 465))
    SMTP_USER   = "torretotld@gmail.com"
    SMTP_PASS   = "ejah tjtv fkpi bmke"
    FROM_ADDR   = SMTP_USER
    TO_ADDRS    = "saintyves.ludovic@gmail.com"

    # Préparer le corps
    body = "Voici la liste des summer internships:\n\n" + \
           "\n".join(f"• {comp} – {title} - {category} - {url}" for comp, title, category, url in open_offers)
    body+="\n Voici la liste des summer internships qui sont déjà ouverts:\n\n"+ \
           "\n".join(f"• {comp} – {title} - {category} - {url}" for comp, title, category, url in old_procs)

    msg = EmailMessage()
    msg["Subject"] = "Nouveau Process Ouvert"
    msg["From"]    = FROM_ADDR
    msg["To"]      = TO_ADDRS
    msg.set_content(body)

    # Envoi en précisant explicitement les destinataires
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg, from_addr=FROM_ADDR, to_addrs=TO_ADDRS)

    print(f"Email envoyé à : {TO_ADDRS}")

def new_process(offres, process):
    new_procs=list()
    old_procs=list()
    for comp, title, category, url in offres:
        a=0
        for i in range(len(procs)):
            if procs[i]["Company"]==comp:
                a+=1
        if a==0:
            new_procs.append((comp,title, category, url))
        else:
            old_procs.append((comp,title, category, url))
    return(new_procs, old_procs)

if __name__ == "__main__":
    # 1) Scrape
    offres = scrape_open_summer_internships()
    procs = read_process_csv("processus_ouverts.csv")
    newprocs, oldprocs=new_process(offres,procs)
    if len(newprocs)>0:
        send_email(newprocs,oldprocs)
    csv_file = ecriture_csv(offres)
