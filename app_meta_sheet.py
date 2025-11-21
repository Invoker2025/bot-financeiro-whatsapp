# ------------------------------------------------------------------------------
# FUNÇÃO DE ENVIO (META WHATSAPP) - VERSÃO SEM CORREÇÃO DO 9
# ------------------------------------------------------------------------------

def send_whatsapp_message(to_number, message):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.error("Faltam credenciais da Meta (Token ou Phone ID)")
        return

    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    # Limpeza do número: Remove + e whatsapp:, mas NÃO adiciona o 9.
    # Se a Meta mandar sem o 9, vamos devolver sem o 9.
    clean_number = to_number.replace("+", "").replace("whatsapp:", "")
    
    logger.info(f"Enviando para o número original: {clean_number}")
    
    data = {
        "messaging_product": "whatsapp",
        "to": clean_number,
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        # Loga a resposta completa da Meta para sabermos se deu erro ou sucesso
        logger.info(f"Resposta da Meta: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Erro requisição Meta: {e}")
