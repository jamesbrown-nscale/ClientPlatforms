output "ip_address" {
  description = "Point an A record for the domain at this."
  value       = google_compute_global_address.lb.address
}

output "url" {
  value = "https://${var.domain}"
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "services" {
  value = { for k, s in google_cloud_run_v2_service.app : k => s.name }
}

output "build_service_account" {
  value = google_service_account.build.id
}

output "device_settings" {
  description = "What to configure on devices."
  value = {
    notifications_server_url = "https://${var.domain}/notifications"
    elevation_url            = "https://${var.domain}/elevation"
  }
}
