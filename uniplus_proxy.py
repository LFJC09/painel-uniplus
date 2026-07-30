#!/usr/bin/env python3
"""
Proxy CORS local para a API do Uniplus (dinâmico por requisição).

Uso simples (recomendado):
    python3 uniplus_proxy.py

Não precisa mais informar --target: o painel HTML manda, em cada chamada,
para qual servidor real deve ir (via cabeçalho X-Proxy-Target) — assim você
digita o endereço real do Uniplus direto no painel, como antes, e o proxy
faz o repasse por baixo dos panos, sem precisar decorar "localhost:8787"
em campo nenhum além do próprio arquivo do painel (que já sabe disso).

Uso com destino fixo (opcional, mantido por compatibilidade):
    python3 uniplus_proxy.py --target https://lfsistemas-05.webuniplus.com

O proxy repassa as chamadas para o servidor real e adiciona os cabeçalhos
de CORS que o navegador exige, mas que a API do Uniplus (feita para uso
servidor-a-servidor) não envia. Ele roda só na sua máquina — nada passa
por fora além da sua própria conexão até o Uniplus.
"""
import argparse
import gzip
import http.server
import os
import socketserver
import ssl
import urllib.error
import urllib.parse
import urllib.request

HOP_BY_HOP = {'host', 'content-length', 'connection', 'transfer-encoding'}
CORS_RESPONSE_HEADERS = {
    'access-control-allow-origin', 'access-control-allow-headers',
    'access-control-allow-methods', 'access-control-allow-credentials',
    'access-control-expose-headers', 'access-control-max-age'
}
TARGET_HEADER = 'X-Proxy-Target'


def _maybe_decompress(payload, headers_obj):
    """Se a resposta veio comprimida mesmo com Accept-Encoding: identity,
    descomprime aqui para nunca repassar bytes ilegíveis ao navegador."""
    encoding = (headers_obj.get('Content-Encoding') or '').lower()
    if encoding == 'gzip':
        try:
            return gzip.decompress(payload)
        except Exception:
            return payload
    return payload


def build_handler(default_target):
    unverified_ctx = ssl._create_unverified_context()

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        def _cors_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            # '*' em Allow-Headers NÃO cobre o cabeçalho Authorization (regra do
            # próprio navegador) — por isso ecoamos de volta exatamente o que o
            # preflight pediu em Access-Control-Request-Headers, que cobre tudo
            # que o painel realmente usa (Authorization, Content-Type, X-Proxy-Target).
            requested = self.headers.get('Access-Control-Request-Headers')
            self.send_header('Access-Control-Allow-Headers',
                              requested if requested else 'Authorization, Content-Type, X-Proxy-Target, Accept')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
            self.send_header('Access-Control-Expose-Headers', '*')
            self.send_header('Access-Control-Max-Age', '86400')

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def _resolve_target(self):
            target = self.headers.get(TARGET_HEADER) or default_target
            if not target:
                return None
            target = target.strip()
            if not (target.startswith('http://') or target.startswith('https://')):
                return None
            return target.rstrip('/')

        def _proxy(self, method):
            target = self._resolve_target()
            if not target:
                self.send_response(400)
                self._cors_headers()
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(
                    ('Nenhum servidor de destino informado. O painel deveria enviar o '
                     f'cabeçalho {TARGET_HEADER}, ou inicie o proxy com --target.').encode('utf-8'))
                return

            url = target + self.path
            body = None
            length = self.headers.get('Content-Length')
            if length:
                body = self.rfile.read(int(length))
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP_BY_HOP
                       and k.lower() != TARGET_HEADER.lower()
                       and k.lower() != 'accept-encoding'}
            # Não anunciamos suporte a gzip/deflate para o servidor real — assim ele
            # devolve a resposta em texto puro, e não corremos o risco de repassar
            # bytes comprimidos com um cabeçalho Content-Encoding desencontrado.
            headers['Accept-Encoding'] = 'identity'

            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            ctx = unverified_ctx if url.lower().startswith('https') else None
            try:
                with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                    payload = _maybe_decompress(resp.read(), resp.headers)
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() in HOP_BY_HOP or k.lower() in CORS_RESPONSE_HEADERS or k.lower() == 'content-encoding':
                            continue
                        self.send_header(k, v)
                    self._cors_headers()
                    self.end_headers()
                    self.wfile.write(payload)
            except urllib.error.HTTPError as e:
                payload = _maybe_decompress(e.read(), e.headers)
                self.send_response(e.code)
                self._cors_headers()
                self.send_header('Content-Type', e.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                self.send_response(502)
                self._cors_headers()
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(('Erro ao repassar para ' + url + ': ' + str(e)).encode('utf-8'))

        def do_GET(self):
            self._proxy('GET')

        def do_POST(self):
            self._proxy('POST')

        def do_PUT(self):
            self._proxy('PUT')

        def do_DELETE(self):
            self._proxy('DELETE')

        def log_message(self, fmt, *args):
            print('[proxy]', fmt % args)

    return ProxyHandler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--target', default=None,
                     help='(Opcional) Endereço fixo do servidor Uniplus, usado só se o painel não informar um.')
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8787)),
                     help='Porta local do proxy (padrão 8787, ou variável de ambiente PORT quando hospedado).')
    ap.add_argument('--host', default='0.0.0.0' if os.environ.get('PORT') else '127.0.0.1',
                     help='Interface de rede (127.0.0.1 localmente, 0.0.0.0 quando hospedado).')
    args = ap.parse_args()

    handler = build_handler(args.target)
    with socketserver.ThreadingTCPServer((args.host, args.port), handler) as httpd:
        if args.target:
            print(f'Proxy rodando em {args.host}:{args.port}  ->  padrão: {args.target}')
        else:
            print(f'Proxy rodando em {args.host}:{args.port}  (destino informado pelo painel a cada chamada)')
        print('Deixe esta janela aberta enquanto usa o painel. Ctrl+C para parar.')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nProxy encerrado.')


if __name__ == '__main__':
    main()
