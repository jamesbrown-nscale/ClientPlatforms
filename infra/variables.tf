variable "project_id" {
  description = "Google Cloud project for Client Platforms."
  type        = string
}

variable "region" {
  description = "Where the services and data live."
  type        = string
  default     = "europe-west2" # London
}

variable "domain" {
  description = "The site's address."
  type        = string
  default     = "clientplatforms.nscale.com"
}

variable "allowed_domain" {
  description = "Google Workspace domain people sign in with. The apps refuse any other."
  type        = string
  default     = "nscale.com"
}

variable "portal_access" {
  description = "Who can open the home page (IAP members, e.g. domain:nscale.com or group:x@nscale.com)."
  type        = list(string)
  default     = ["domain:nscale.com"]
}

variable "notifications_access" {
  description = "Who can open IT Notifications (IAP members). The app also applies notify_admin_emails if set."
  type        = list(string)
}

variable "elevation_access" {
  description = "Who can read the admin elevation log (IAP members)."
  type        = list(string)
}

variable "notify_admin_emails" {
  description = "Optional comma-separated list of who may send notifications, on top of notifications_access."
  type        = string
  default     = ""
}

variable "notify_allowed_link_hosts" {
  description = "Domains notification links may point to."
  type        = string
  default     = "nscale.com"
}

variable "device_rate_limit_per_minute" {
  description = "Most requests a single address may make to the device endpoints per minute."
  type        = number
  default     = 3000
}

variable "backup_retention_days" {
  description = "How long daily Firestore backups are kept."
  type        = number
  default     = 14
}

variable "deletion_protection" {
  description = "Stops Terraform deleting the databases and services."
  type        = bool
  default     = true
}

variable "dns_managed_zone" {
  description = "Cloud DNS zone in this project to create the A record in. Leave empty if DNS is elsewhere."
  type        = string
  default     = ""
}
