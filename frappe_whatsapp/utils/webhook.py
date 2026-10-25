"""Webhook."""
import frappe
import json
import requests
import time
from frappe import _
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()
	return post()


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	verify_token = frappe.form_dict.get("hub.verify_token")
	webhook_verify_token = frappe.db.get_value(
		'WhatsApp Account',
		{"webhook_verify_token": verify_token},
		'webhook_verify_token'
	)
	if not webhook_verify_token:
		frappe.throw("No matching WhatsApp account")

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def post():
	"""Post."""
	data = frappe.local.form_dict
	frappe.get_doc({
		"doctype": "WhatsApp Notification Log",
		"template": "Webhook",
		"meta_data": json.dumps(data)
	}).insert(ignore_permissions=True)

	messages = []
	phone_id = None
	try:
		messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
		phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get("phone_number_id")
	except KeyError:
		messages = data["entry"]["changes"][0]["value"].get("messages", [])
	sender_profile_name = next(
		(
			contact.get("profile", {}).get("name")
			for entry in data.get("entry", [])
			for change in entry.get("changes", [])
			for contact in change.get("value", {}).get("contacts", [])
		),
		None,
	)

	whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None

	# Only `messages` events carry `metadata.phone_number_id`. Status-change
	# events (`message_template_status_update`, message status callbacks) have
	# no metadata, so `phone_id` is None and `whatsapp_account` is also None
	# for them by design. Gating the entire handler on `whatsapp_account`
	# silently drops every template-status update; gate only the message-
	# ingestion branch instead.
	if messages and not whatsapp_account:
		return

	if messages:
		for message in messages:
			message_type = message['type']
			is_reply = True if message.get('context') and 'forwarded' not in message.get('context') else False
			reply_to_message_id = message['context']['id'] if is_reply else None

			# PRODSIA: inbound structured messages
			if message_type in ("location", "contacts"):
				# Evitar duplicados cuando Meta reenvía el webhook.
				if frappe.db.exists("WhatsApp Message", {"message_id": message["id"]}):
					continue

				if message_type == "location":
					loc = message.get("location") or {}
					lat = loc.get("latitude")
					lon = loc.get("longitude")
					if lat is None or lon is None:
						continue

					body = f"https://www.google.com/maps?q={lat},{lon}"
					content_type = "location"

				else:
					parts = []
					for contact in message.get("contacts", []):
						name = contact.get("name") or {}
						full_name = name.get("formatted_name") or "Sin nombre"
						phones = [
							p.get("phone") or p.get("wa_id")
							for p in contact.get("phones", [])
						]
						phones = [str(p) for p in phones if p]
						parts.append(
							full_name + ("\n" + "\n".join(phones) if phones else "")
						)

					body = "\n\n".join(parts) or "Contacto compartido"
					content_type = "contact"

				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message["from"],
					"message_id": message["id"],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"message": body,
					"content_type": content_type,
					"profile_name": sender_profile_name,
					"whatsapp_account": whatsapp_account.name,
				}).insert(ignore_permissions=True)
				continue

			# Respuestas interactivas no contempladas en el webhook original.
			if message_type == "interactive":
				interactive = message.get("interactive") or {}
				if interactive.get("type") not in (
					"button_reply", "list_reply", "nfm_reply"
				):
					if frappe.db.exists("WhatsApp Message", {"message_id": message["id"]}):
						continue
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message["from"],
						"message_id": message["id"],
						"message": json.dumps(interactive, ensure_ascii=False),
						"content_type": "interactive",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name,
					}).insert(ignore_permissions=True)
					continue

			# PRODSIA_LOCATION_CONTACTS_V2
			if message_type in ("location", "contacts"):
				if frappe.db.exists(
					"WhatsApp Message",
					{"message_id": message["id"]}
				):
					continue

				if message_type == "location":
					location = message.get("location") or {}
					lat = location.get("latitude")
					lon = location.get("longitude")

					if lat is None or lon is None:
						frappe.logger("whatsapp").warning(
							"Ubicación sin coordenadas: %s", message.get("id")
						)
						continue

					text = f"https://www.google.com/maps?q={lat},{lon}"

					if location.get("name"):
						text += "\n" + location["name"]

					if location.get("address"):
						text += "\n" + location["address"]

					content_type = "location"

				else:
					contacts = message.get("contacts") or []
					lines = []

					for contact in contacts:
						name = contact.get("name") or {}
						full_name = name.get("formatted_name")

						if not full_name:
							full_name = " ".join(
								str(name.get(k) or "")
								for k in ("first_name", "last_name")
							).strip() or "Sin nombre"

						phones = [
							str(phone.get("phone") or phone.get("wa_id"))
							for phone in contact.get("phones", [])
							if phone.get("phone") or phone.get("wa_id")
						]

						lines.append(
							full_name +
							("\n" + "\n".join(phones) if phones else "")
						)

					text = "\n\n".join(lines) or "Contacto compartido"
					content_type = "contact"

				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message["from"],
					"message_id": message["id"],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"message": text,
					"content_type": content_type,
					"profile_name": sender_profile_name,
					"whatsapp_account": whatsapp_account.name,
				}).insert(ignore_permissions=True)

				continue

			if message_type == 'text':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['text']['body'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type":message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'reaction':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['reaction']['emoji'],
					"reply_to_message_id": message['reaction']['message_id'],
					"message_id": message['id'],
					"content_type": "reaction",
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'interactive':
				interactive_data = message['interactive']
				interactive_type = interactive_data.get('type')

				# Handle button reply
				if interactive_type == 'button_reply':
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": interactive_data['button_reply']['id'],
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "button",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)
				# Handle list reply
				elif interactive_type == 'list_reply':
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": interactive_data['list_reply']['id'],
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "button",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)
				# Handle WhatsApp Flows (nfm_reply)
				elif interactive_type == 'nfm_reply':
					nfm_reply = interactive_data['nfm_reply']
					response_json_str = nfm_reply.get('response_json', '{}')

					# Parse the response JSON
					try:
						flow_response = json.loads(response_json_str)
					except json.JSONDecodeError:
						flow_response = {}

					# Create a summary message from the flow response
					summary_parts = []
					for key, value in flow_response.items():
						if value:
							summary_parts.append(f"{key}: {value}")
					summary_message = ", ".join(summary_parts) if summary_parts else "Flow completed"

					msg_doc = frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": summary_message,
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "flow",
						"flow_response": json.dumps(flow_response),
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)

					# Publish realtime event for flow response
					frappe.publish_realtime(  # nosemgrep: frappe-realtime-pick-room -- intentional site-wide fan-out for chat UIs (whatsapp_chat companion app) listening for inbound flow responses
						"whatsapp_flow_response",
						{
							"phone": message['from'],
							"message_id": message['id'],
							"flow_response": flow_response,
							"whatsapp_account": whatsapp_account.name
						}
					)
			# NEW: Handle Shopping Cart / Orders from MPM
			elif message_type == 'order':
				order_data = message['order']

				# Inject the raw data into product_catalog_json
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": _("New Order Received via WhatsApp"),
					"message_id": message['id'],
					"content_type": "order",
					"profile_name": sender_profile_name,
					"whatsapp_account": whatsapp_account.name,
					"product_catalog_json": json.dumps(order_data)
				}).insert(ignore_permissions=True)
			elif message_type in ["image", "audio", "video", "document"]:
				token = whatsapp_account.get_password("token")
				url = f"{whatsapp_account.url}/{whatsapp_account.version}/"

				media_id = message[message_type]["id"]
				headers = {
					'Authorization': 'Bearer ' + token

				}
				response = requests.get(f'{url}{media_id}/', headers=headers)

				if response.status_code == 200:
					media_data = response.json()
					media_url = media_data.get("url")
					mime_type = media_data.get("mime_type")
					file_extension = mime_type.split('/')[1]

					media_response = requests.get(media_url, headers=headers)
					if media_response.status_code == 200:

						file_data = media_response.content
						file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

						message_doc = frappe.get_doc({
							"doctype": "WhatsApp Message",
							"type": "Incoming",
							"from": message['from'],
							"message_id": message['id'],
							"reply_to_message_id": reply_to_message_id,
							"is_reply": is_reply,
							"message": message[message_type].get("caption", ""),
							"content_type" : message_type,
							"profile_name":sender_profile_name,
							"whatsapp_account":whatsapp_account.name
						}).insert(ignore_permissions=True)

						file = frappe.get_doc(
							{
								"doctype": "File",
								"file_name": file_name,
								"attached_to_doctype": "WhatsApp Message",
								"attached_to_name": message_doc.name,
								"content": file_data,
								"attached_to_field": "attach"
							}
						).save(ignore_permissions=True)


						message_doc.attach = file.file_url
						message_doc.save()
			elif message_type == "button":
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['button']['text'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type": message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			else:
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message_id": message['id'],
					"message": message[message_type].get(message_type),
					"content_type" : message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

	else:
		changes = None
		try:
			changes = data["entry"][0]["changes"][0]
		except KeyError:
			changes = data["entry"]["changes"][0]
		update_status(changes)
	return

def update_status(data):
	"""Update status hook."""
	if data.get("field") == "message_template_status_update":
		update_template_status(data['value'])

	elif data.get("field") == "messages":
		update_message_status(data['value'])

def update_template_status(data):
	"""Update template status."""
	frappe.db.sql(
		"""UPDATE `tabWhatsApp Templates`
		SET status = %(event)s
		WHERE id = %(message_template_id)s""",
		data
	)

def update_message_status(data):
	"""Update message status."""
	id = data['statuses'][0]['id']
	status = data['statuses'][0]['status']
	conversation = data['statuses'][0].get('conversation', {}).get('id')
	name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

	doc = frappe.get_doc("WhatsApp Message", name)
	doc.status = status
	if conversation:
		doc.conversation_id = conversation
	doc.save(ignore_permissions=True)
