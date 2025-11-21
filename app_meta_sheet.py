import os
import sys
import json
import logging
from datetime import datetime
import requests
from flask import Flask, request, jsonify
import openai
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# CONFIGURAÇÕES GERAIS
# ==============================================================================

# ID da sua Planilha Google (Copie do link da sua planilha)
SHEET_ID = "1UfAxtLmB5LKNGoIOhue5jruSOA-AyC3CCQK6-P5YGvc"

# Verifica onde está o arquivo de credenciais do Google (Render ou Local)
if os.path.exists("/etc/secrets/google-creds.json"):
    GOOGLE_SA_JSON_PATH = "/etc/secrets/google-creds.json"
else:
    GOOGLE_SA_JSON_PATH = "google-creds.json"

# --- CHAVES DE API (Buscadas nas Variáveis de Ambiente do Render) ---
# ATENÇÃO: Nunca escreva suas senhas diretamente aqui no código.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")

app = Flask(__name__)

# Configuração de Logs (Para ver o que acontece no painel do Render)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==============================================================================
# FUNÇÕES AUXILIARES
# ==============================================================================

def normalize_text(text):
    """Remove acentos e caracteres especiais para padronizar o texto."""
    import unicodedata
    if not text:
        return "Outros"
    try:
        return unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII').title()
    except:
        return str(text).title()

def get_gspread_client():
    """Autentica no Google Sheets usando o arquivo de credenciais."""
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(
            GOOGLE_SA_JSON_PATH, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"Erro na autenticação do Google Sheets: {e}")
        return None

def parse_expense_openai(text):
    """Usa o ChatGPT para extrair valor, categoria e nota da mensagem."""
    try:
        prompt = f"""
        Analise a despesa ou receita: "{text}".
        Retorne apenas um JSON com as chaves:
        - amount (float, use ponto para decimais)
        - category (string, ex: Alimentação, Transporte)
        - note (string, descrição curta)
        - payment (string, ex: Pix, Crédito, Dinheiro)
        - type (expense ou income)
        
        Exemplo de resposta: 
        {{"amount": 50.0, "category": "Alimentação", "note": "Pizza", "payment": "Crédito", "type": "expense"}}
        """
        
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=150
        )
        
        content = resp.choices[0].message.content.strip()
        
        # Tenta encontrar e extrair o JSON da resposta da IA
        s = content.find('{')
        e = content.rfind('}') + 1
        if s != -1 and e > s:
            data = json.loads(content[s:e])
            return (
                float(data.get("amount", 0.0)),
                normalize_text(data.get("category", "Outros")),
                str(data.get("note", text)),
                normalize_text(data.get("payment", "Outros")),
                str(data.get("type", "expense"))
            )
        # Fallback se a IA não retornar JSON
        return 0.0, "Geral", text, "Outros", "expense"
        
    except Exception as e:
        logger.error(f"Erro na OpenAI: {e}")
        return 0.0, "Geral", text, "Outros", "expense"

# ==============================================================================
# FUNÇÃO DE ENVIO DE MENSAGEM (WHATSAPP CLOUD API)
# ==============================================================================

def send_whatsapp_message(to_number, message):
    """Envia mensagem de texto via WhatsApp Cloud API da Meta."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.error("Faltam credenciais da Meta (Token ou Phone ID). Verifique no Render.")
        return

    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    # Limpa o número (remove caracteres especiais)
    clean_number = to_number.replace("+", "").replace("whatsapp:", "").strip()
    
    # --- CORREÇÃO DE NÚMERO BRASILEIRO (ADICIONA O 9º DÍGITO) ---
    # Se for Brasil (55) e tiver 12 dígitos (Ex: 55 75 9223 8338), adiciona o 9.
    if clean_number.startswith("55") and len(clean_number) == 12:
        corrected_number = clean_number[:4] + "9" + clean_number[4:]
        logger.info(f"Corrigindo número: {clean_number} -> {corrected_number}")
        clean_number = corrected_number
    
    data = {
        "messaging_product": "whatsapp",
        "to": clean_number,
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        
        if response.status_code in [200, 201]:
            logger.info(f"Mensagem enviada com sucesso para {clean_number}!")
        else:
            logger.error(f"Erro Meta ao enviar: {response.status_code} - {response.text}")
            
    except Exception as e:
        logger.error(f"Erro na requisição Meta: {e}")

# ==============================================================================
# ROTAS DO SERVIDOR (WEBHOOK)
# ==============================================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificação inicial exigida pela Meta para conectar o Webhook."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("WEBHOOK VERIFICADO COM SUCESSO!")
        return challenge, 200
    else:
        return "Erro de verificação de token", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    """Recebe notificações de mensagens novas do WhatsApp."""
    try:
        body = request.get_json()

        # Verifica se é um evento de mensagem do WhatsApp
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    # Garante que é uma mensagem de texto recebida (não status de entrega)
                    if "messages" in value:
                        message = value["messages"][0]
                        from_number = message["from"]
                        
                        # Processa apenas mensagens de texto
                        if message["type"] == "text":
                            msg_body = message["text"]["body"]
                            logger.info(f"Mensagem recebida de {from_number}: {msg_body}")

                            # 1. Processar Texto com IA
                            amount, category, note, payment, t_type = parse_expense_openai(msg_body)

                            # 2. Salvar na Planilha
                            gc = get_gspread_client()
                            if gc:
                                sh = gc.open_by_key(SHEET_ID)
                                try:
                                    # Tenta achar aba 'Extrato_Geral', se não, pega a primeira
                                    ws = sh.worksheet("Extrato_Geral")
                                except:
                                    ws = sh.get_worksheet(0)

                                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                # Formata valor para planilha (virgula como decimal)
                                val_fmt = f"{-abs(amount) if t_type == 'expense' else abs(amount):.2f}".replace(".", ",")

                                ws.append_row([timestamp, val_fmt, category, note, payment, t_type, msg_body])
                                logger.info("Dados salvos na planilha.")

                                # 3. Responder no WhatsApp
                                reply_text = f"✅ Salvo!\nR$ {amount:.2f} ({category})"
                                send_whatsapp_message(from_number, reply_text)
                        else:
                            logger.info("Mensagem recebida não é texto (ignorada).")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"Erro crítico no webhook: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
