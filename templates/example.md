# Izdavatelj / Issuer
```
Example d.o.o.

OIB  : 12345678901
IBAN : HR1234567890123456789
Tel  : +385 1 2345 678
Email: contact@example.com
```
Web  : [https://example.com](https://example.com)

# Kupac / Client
```
Client Name
Address Line 1
```

# Stavke / Items
1. Order ID: {{ order_id }}
   Ukupno bez PDV-a: €{{ amount }}
   PDV (0%): €0.00

Napomena: oslobođeno PDV-a prema članku 17. stavak 1. Zakona o PDV-u (reverse charge).

# Račun / Fiscal Receipt
```
Broj računa              : {{ receipt_number }}
Oznaka naplatnog uređaja : {{ register_id }}
Oznaka poslovnog prostora: {{ location_id }}
Datum i vrijeme izdavanja: {{ payment_time }}
Način plaćanja           : Kartice (B - bezgotovinsko)
Valuta                   : EUR
Ukupan iznos             : {{ amount }}
PDV sustav               : Obveznik PDV-a (reverse charge)

ZKI                      : {{ zki }}
JIR                      : {{ jir }}

Račun izdao              : Example d.o.o., direktor Ime Prezime
```
Račun je fiskaliziran sukladno Zakonu o fiskalizaciji u prometu gotovinom.

# Provjera QR koda / QR code check
{{ qr_code }}

<br>
{{ verification_link }}
