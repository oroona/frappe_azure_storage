# Copyright (c) 2022, Lovin Maxwell and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import os

import frappe
from frappe import _
from frappe.utils import cint
from frappe.model.document import Document
from azure.storage.blob import ContainerClient
from rq.timeouts import JobTimeoutException
from frappe.integrations.offsite_backup_utils import (
	generate_files_backup,
	get_latest_backup_file,
	send_email,
	validate_file_size,
)
from frappe.utils.background_jobs import enqueue

class AzureStorageSettings(Document):
	def validate(self):
		if not self.enabled:
			return

	@frappe.whitelist()
	def back_up_azure(self,retry_count=0):
		take_backups_azure(retry_count)


@frappe.whitelist()
def take_backup():
	"""Enqueue long job for taking backup to Azure"""
	enqueue(
		"frappe_azure_storage.frappe_azure_storage.doctype.azure_storage_settings.azure_storage_settings.take_backups_azure",
		queue="long",
		timeout=1500,
	)
	frappe.msgprint(_("Queued for backup. It may take a few minutes to an hour."))


def take_backups_daily():
	take_backups_if("Daily")


def take_backups_weekly():
	take_backups_if("Weekly")


def take_backups_monthly():
	take_backups_if("Monthly")

def take_backups_if(freq):
	if cint(frappe.db.get_value("Azure Storage Settings", None, "enabled")):
		if frappe.db.get_value("Azure Storage Settings", None, "frequency") == freq:
			take_backups_azure()


@frappe.whitelist()
def take_backups_azure(retry_count=0):
	try:
		validate_file_size()
		backup_to_azure()
		send_email(True, "Azure Storage", "Azure Storage Settings", "notify_email")
	except JobTimeoutException:
		if retry_count < 2:
			args = {"retry_count": retry_count + 1}
			enqueue(
				"frappe_azure_storage.frappe_azure_storage.doctype.azure_storage_settings.azure_storage_settings.take_backups_azure",
				queue="long",
				timeout=1500,
				**args
			)
		else:
			notify()
	except Exception:
		notify()


def notify():
	error_message = frappe.get_traceback()
	send_email(False, "Azure Storage", "Azure Storage Settings", "notify_email", error_message)


def backup_to_azure():
	from frappe.utils import get_backups_path
	

	doc = frappe.get_single("Azure Storage Settings")
	container = doc.default_container
	backup_files = cint(doc.backup_files)

	conn = ContainerClient.from_connection_string(doc.endpoint_url, container_name=container)

	if frappe.flags.create_new_backup:
		from frappe.utils.backups import new_backup
		backup = new_backup(
			ignore_files=False,
			backup_path_db=None,
			backup_path_files=None,
			backup_path_private_files=None,
			ignore_conf=True,
			force=True,
		)
		db_filename = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_db))
		site_config = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_conf))
		if backup_files:
			files_filename = os.path.join(get_backups_path(), os.path.basename(backup.backup_path_files))
			private_files = os.path.join(
				get_backups_path(), os.path.basename(backup.backup_path_private_files)
			)
	else:
		if backup_files:
			db_filename, site_config, files_filename, private_files = get_latest_backup_file(
				with_files=backup_files
			)

			if not files_filename or not private_files:
				generate_files_backup()
				db_filename, site_config, files_filename, private_files = get_latest_backup_file(
					with_files=backup_files
				)

		else:
			db_filename, site_config = get_latest_backup_file()

	folder = os.path.basename(db_filename)[:15] + "/"
	# for adding datetime to folder name

	upload_file_to_azure(db_filename, folder, conn)
	upload_file_to_azure(site_config, folder, conn)

	if backup_files:
		if private_files:
			upload_file_to_azure(private_files, folder, conn)

		if files_filename:
			upload_file_to_azure(files_filename, folder, conn)


def upload_file_to_azure(filename, folder, conn):
	destpath = os.path.join(folder, os.path.basename(filename))
	try:
		site_name = frappe.local.site
		print("Uploading file:", filename)
		# Instantiate a new BlobClient
		blob_client = conn.get_blob_client(f"{site_name}/backup/{destpath}")
		# [START upload_a_blob]
		# Upload content to block blob
		with open(filename, "rb") as data:
			blob_client.upload_blob(data, blob_type="BlockBlob")

	except Exception as e:
		frappe.log_error()
		print("Error uploading: %s" % (e))