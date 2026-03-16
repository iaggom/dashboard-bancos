import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"

tests = [
    f"{BASE}/IfDataCadastro(AnoMes=202506)?$format=json&$top=1",
    f"{BASE}/ListaDeRelatorio()?$format=json&$top=1",
    f"{BASE}/IfDataValores(AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)?@AnoMes=202506&@TipoInstituicao=2&@Relatorio='1'&$format=json&$top=1",
]

for url in tests:
    print("\n" + "=" * 120)
    print("TESTANDO:", url)
    try:
        r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
        print("STATUS:", r.status_code)
        print("URL FINAL:", r.url)
        print("BODY:", r.text[:1000])
    except Exception as e:
        print("ERRO:", type(e).__name__, e)