import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import smtplib
import hashlib
import mimetypes
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication

# Конфигурация из GitHub Secrets / Environment Variables
GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASSWORD = os.getenv('GMAIL_PASSWORD')
RECIPIENT_EMAIL = os.getenv('RECIPIENT_EMAIL')
YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN')
STATE_FILE = 'bbc_state.json'
MAX_EMAIL_SIZE_BYTES = 24 * 1024 * 1024  # Лимит 24 МБ для вложений Gmail

def get_gmt5_now():
    return datetime.utcnow() + timedelta(hours=5)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def upload_to_yandex_disk(file_url, file_name, headers):
    """Загружает файл на Яндекс.Диск через API и возвращает публичную ссылку"""
    if not YANDEX_DISK_TOKEN:
        print("Ошибка: YANDEX_DISK_TOKEN отсутствует в Secrets.")
        return None
        
    ya_headers = {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}
    upload_url_api = "https://cloud-api.yandex.net/v1/disk/resources/upload"
    params = {"path": f"disk/BBC_Videos/{file_name}", "overwrite": "true"}
    
    try:
        requests.put("https://cloud-api.yandex.net/v1/disk/resources", params={"path": "disk/BBC_Videos"}, headers=ya_headers)
        res = requests.get(upload_url_api, params=params, headers=ya_headers)
        if res.status_code != 200:
            return None
            
        upload_url = res.json().get("href")
        file_res = requests.get(file_url, headers=headers, stream=True)
        put_res = requests.put(upload_url, data=file_res.iter_content(chunk_size=1024*1024))
        
        if put_res.status_code == 201:
            pub_url_api = "https://cloud-api.yandex.net/v1/disk/resources/publish"
            requests.put(pub_url_api, params={"path": f"disk/BBC_Videos/{file_name}"}, headers=ya_headers)
            
            meta_url_api = "https://cloud-api.yandex.net/v1/disk/resources"
            meta_res = requests.get(meta_url_api, params={"path": f"disk/BBC_Videos/{file_name}"}, headers=ya_headers)
            return meta_res.json().get("public_url")
    except Exception as e:
        print(f"Ошибка загрузки на Яндекс.Диск: {e}")
    return None

def process_media_and_attachments(soup, headers):
    """Обрабатывает картинки и видео (вложения или Я.Диск)"""
    attachments = []
    image_counter = 0
    video_counter = 0
    
    for img in soup.find_all('img'):
        src = img.get('src')
        if not src:
            continue
        if not src.startswith('http'):
            src = f"https://www.bbc.com{src}"
            
        try:
            img_res = requests.get(src, headers=headers, timeout=10)
            if img_res.status_code == 200:
                content_type, _ = mimetypes.guess_type(src)
                if not content_type: content_type = 'image/jpeg'
                
                cid = f"inline_img_{image_counter}"
                _, subtype = content_type.split('/', 1)
                msg_img = MIMEImage(img_res.content, _subtype=subtype)
                msg_img.add_header('Content-ID', f'<{cid}>')
                msg_img.add_header('Content-Disposition', 'inline')
                
                img['src'] = f"cid:{cid}"
                attachments.append(msg_img)
                image_counter += 1
        except Exception as e:
            print(f"Не удалось скачать картинку {src}: {e}")
            
    for video_tag in soup.find_all(['video', 'source']):
        video_url = video_tag.get('src') or video_tag.get('data-src')
        if not video_url:
            continue
        if not video_url.startswith('http'):
            video_url = f"https://www.bbc.com{video_url}"
            
        try:
            head_res = requests.head(video_url, headers=headers, allow_redirects=True)
            file_size = int(head_res.headers.get('Content-Length', 0))
            file_name = f"video_{video_counter}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            
            if 0 < file_size < MAX_EMAIL_SIZE_BYTES:
                vid_res = requests.get(video_url, headers=headers)
                if vid_res.status_code == 200:
                    msg_vid = MIMEApplication(vid_res.content, _subtype="mp4")
                    msg_vid.add_header('Content-Disposition', 'attachment', filename=file_name)
                    attachments.append(msg_vid)
                    print(f"Видео {file_name} добавлено во вложение.")
            elif file_size >= MAX_EMAIL_SIZE_BYTES:
                public_link = upload_to_yandex_disk(video_url, file_name, headers)
                if public_link:
                    link_tag = soup.new_tag('a', href=public_link)
                    link_tag.string = f"📥 Скачать тяжелое видео со статьи на Яндекс.Диске ({file_size // (1024*1024)} МБ)"
                    div_box = soup.new_tag('div', style="padding:15px; background:#fff3cd; border:1px solid #ffeeba; margin:15px 0; font-family:sans-serif;")
                    div_box.append(link_tag)
                    video_tag.insert_after(div_box)
                    print(f"Видео загружено на Яндекс.Диск: {public_link}")
            video_counter += 1
        except Exception as e:
            print(f"Ошибка обработки видео {video_url}: {e}")
            
    return attachments

def fix_image_containers_and_styles(soup):
    """Удаляет деструктивные адаптивные стили врапперов и фиксит отображение картинок"""
    for div in soup.find_all('div', style=True):
        if 'padding-bottom' in div['style'] or 'background-' in div['style']:
            div['style'] = "display: block; width: auto; height: auto; padding: 0; margin: 10px 0;"
            
    for img in soup.find_all('img'):
        img['style'] = "max-width: 100%; height: auto; display: block; margin: 0 auto;"
        if img.get('width'): del img['width']
        if img.get('height'): del img['height']

def send_email_with_limit_control(html_content_soup, headers):
    if not all([GMAIL_USER, GMAIL_PASSWORD, RECIPIENT_EMAIL]):
        print("Ошибка: Отсутствуют учетные данные почты.")
        return False

    table_styles = """
    <style>
        table { border-collapse: collapse; width: 100%; margin: 15px 0; font-family: sans-serif; font-size: 14px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; font-weight: bold; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        img { max-width: 100% !important; height: auto !important; }
    </style>
    """
    
    fix_image_containers_and_styles(html_content_soup)
    attachments = process_media_and_attachments(html_content_soup, headers)
    final_html_text = table_styles + str(html_content_soup)

    msg = MIMEMultipart('related')
    msg['Subject'] = "BBC"
    msg['From'] = GMAIL_USER
    msg['To'] = RECIPIENT_EMAIL

    msg_alternative = MIMEMultipart('alternative')
    msg.attach(msg_alternative)
    msg_html = MIMEText(final_html_text, 'html', 'utf-8')
    msg_alternative.attach(msg_html)

    for att_obj in attachments:
        msg.attach(att_obj)

    raw_message = msg.as_string()
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, raw_message)
        print("Письмо успешно отправлено.")
        return True
    except Exception as e:
        print(f"Ошибка отправки почты: {e}")
        return False

def clean_and_extract_content(soup):
    """Очищает текст от мусора, вырезает WhatsApp-промо, соцсети и удаляет плашки времени"""
    
    # 1. ВЫРЕЗАЕМ ВЕСЬ ЭЛЕМЕНТ ПРОМО-БЛОКА WHATSAPP
    for wa_element in soup.find_all(lambda tag: tag and tag.name in ['div', 'section']):
        if not hasattr(wa_element, 'attrs') or wa_element.attrs is None:
            continue
        if (wa_element.get('class') and 'css-1uasm9p' in wa_element.get('class')) or \
           ("whatsapp.com/channel/" in str(wa_element.get('href', '')) or "Канал Би-би-си в WhatsApp" in wa_element.text):
            wa_element.decompose()

    # 2. ВЫРЕЗАЕМ БЛОК С СОЦСЕТЯМИ И РАССЫЛКОЙ (article-links-block)
    for soc_element in soup.find_all('section', attrs={"data-e2e": "article-links-block"}):
        soc_element.decompose()

    # 3. ИГНОРИРОВАНИЕ ДАТЫ И ВРЕМЕНИ ОБНОВЛЕНИЯ
    for time_tag in soup.find_all(['time', 'span', 'div']):
        if not hasattr(time_tag, 'attrs') or time_tag.attrs is None:
            continue
        t_class = "".join(time_tag.get('class', []))
        if (time_tag.get('data-testid') == 'timestamp' or 
            'timestamp' in t_class.lower() or 
            'Timestamp' in t_class or 
            time_tag.name == 'time'):
            time_tag.decompose()

    # Удаление рекомендаций
    for element in soup.find_all(lambda tag: tag and tag.name in ['div', 'section', 'p', 'ul']):
        if not hasattr(element, 'attrs') or element.attrs is None:
            continue
        if (element.get('id') == 'end-of-recommendations' or 
            'end-of-recommendations' in str(element.get('href', '')) or
            'recommendations-heading' in str(element.get('id', '')) or
            element.get('data-e2e') == 'recommendations-wrapper'):
            
            parent_container = element.find_parent(class_=lambda c: c and 'css-' in c)
            if parent_container: parent_container.decompose()
            else: element.decompose()

    for block in soup.find_all(['div', 'section']):
        if not hasattr(block, 'attrs') or block.attrs is None:
            continue
        if block and hasattr(block, 'text') and block.text:
            if "Самое популярное" in block.text and ("Skip" in block.text or "End of" in block.text):
                block.decompose()

    main_content = soup.find('main') or soup.find('article')
    if not main_content:
        return None
    
    for element in main_content.find_all(['script', 'style', 'nav', 'button', 'form']):
        element.decompose()
        
    for share in main_content.find_all(attrs={"data-component": "share-tools"}):
        share.decompose()
    for promo in main_content.find_all(attrs={"data-component": "promo"}):
        promo.decompose()
        
    return main_content

def parse_bbc_russian():
    url = "https://www.bbc.com/russian"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"Ошибка при запросе к главной странице BBC: {e}")
        return
        
    soup = BeautifulSoup(response.text, 'html.parser')
    
    for popular_section in soup.find_all(lambda tag: tag and tag.name in ['div', 'section', 'nav']):
        if popular_section and hasattr(popular_section, 'text') and popular_section.text:
            if "Самое популярное" in popular_section.text:
                popular_section.decompose()

    state = load_state()
    new_state = {}
    
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/russian/articles/' in href:
            links.add(('article', href if href.startswith('http') else f"https://www.bbc.com{href}"))
        elif '/russian/live/' in href:
            links.add(('live', href if href.startswith('http') else f"https://www.bbc.com{href}"))

    for type_, link in links:
        item_id = link.split('/')[-1]
        print(f"Обработка {type_}: {link}")
        
        try:
            item_res = requests.get(link, headers=headers)
            item_res.raise_for_status()
            item_soup = BeautifulSoup(item_res.text, 'html.parser')
        except Exception as e:
            print(f"Ошибка загрузки страницы {link}: {e}")
            continue
            
        if type_ == 'article':
            content_node = clean_and_extract_content(item_soup)
            if not content_node:
                continue
                
            content_text = " ".join(content_node.text.split())
            content_hash = hashlib.md5(content_text.encode('utf-8')).hexdigest()
            old_item = state.get(item_id)
            
            if not old_item:
                email_soup = BeautifulSoup(str(content_node), 'html.parser')
                if send_email_with_limit_control(email_soup, headers):
                    new_state[item_id] = {'type': 'article', 'hash': content_hash, 'text': content_text, 'html': str(content_node)}
            else:
                new_state[item_id] = old_item
                    
        # =====================================================================
        # БЛОК ОТСЛЕЖИВАНИЯ LIVE ВРЕМЕННО ЗАКОММЕНТИРОВАН ПО ТРЕБОВАНИЮ
        # =====================================================================
        # elif type_ == 'live':
        #     blocks = item_soup.find_all(attrs={"data-component": "live-reporter-block"}) or item_soup.find_all('article')
        #     old_item = state.get(item_id, {'type': 'live', 'sent_blocks': []})
        #     sent_blocks = old_item.get('sent_blocks', [])
        #     current_sent_blocks = list(sent_blocks)
        #     
        #     new_updates = []
        #     seen_texts_this_run = set()
        #     
        #     for block in blocks:
        #         if not block:
        #             continue
        #         
        #         block_soup = BeautifulSoup(str(block), 'html.parser')
        #         cleaned_block = clean_and_extract_content(block_soup)
        #         if not cleaned_block:
        #             cleaned_block = block_soup
        #         
        #         pure_text = " ".join(cleaned_block.text.split())
        #         if not pure_text or pure_text in seen_texts_this_run:
        #             continue
        #         
        #         block_id = hashlib.md5(pure_text.encode('utf-8')).hexdigest()
        #         
        #         if block_id not in sent_blocks:
        #             seen_texts_this_run.add(pure_text)
        #             
        #             time_element = (cleaned_block.find(attrs={"data-testid": "timestamp"}) or 
        #                             cleaned_block.find('time') or 
        #                             cleaned_block.find(class_=re.compile(r'Timestamp')))
        #             
        #             if time_element:
        #                 br_tag = cleaned_block.new_tag('br')
        #                 time_element.insert_after(br_tag)
        #             
        #             block_content_html = str(cleaned_block)
        #             
        #             update_html = f"<div style='border-left: 4px solid #b00; padding-left: 15px; margin-bottom: 25px;'>"
        #             update_html += f"<div>{block_content_html}</div></div>"
        #             
        #             new_updates.append(update_html)
        #             current_sent_blocks.append(block_id)
        #     
        #     if new_updates:
        #         combined_html = "".join(new_updates)
        #         email_soup = BeautifulSoup(combined_html, 'html.parser')
        #         if send_email_with_limit_control(email_soup, headers):
        #             new_state[item_id] = {'type': 'live', 'sent_blocks': current_sent_blocks}
        #     else:
        #         new_state[item_id] = old_item
        # =====================================================================

    for k, v in state.items():
        if k not in new_state:
            new_state[k] = v
            
    save_state(new_state)

if __name__ == '__main__':
    parse_bbc_russian()
