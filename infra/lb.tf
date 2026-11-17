# ---------- Load balancer, certificate and IAP ----------

resource "google_compute_global_address" "lb" {
  project    = var.project_id
  name       = "client-platforms"
  depends_on = [google_project_service.apis]
}

resource "google_compute_managed_ssl_certificate" "cert" {
  project = var.project_id
  name    = "client-platforms-${replace(var.domain, ".", "-")}"
  managed {
    domains = [var.domain]
  }
  lifecycle {
    create_before_destroy = true
  }
}

# Serverless NEGs. The names refer to the Cloud Run services by name (not by resource) on purpose:
# the services need the backend IDs for IAP_AUDIENCE, so a resource reference would be a cycle.
# Each device endpoint gets its own NEG and backend so IAP can be off for it alone.
locals {
  negs = {
    portal           = local.services.portal.service_name
    notify           = local.services.notify.service_name
    notify_devices   = local.services.notify.service_name
    elevation        = local.services.elevation.service_name
    elevation_ingest = local.services.elevation.service_name
  }
  people_backends = ["portal", "notify", "elevation"]
  device_backends = ["notify_devices", "elevation_ingest"]
  access = {
    portal    = var.portal_access
    notify    = var.notifications_access
    elevation = var.elevation_access
  }
}

resource "google_compute_region_network_endpoint_group" "neg" {
  for_each              = local.negs
  project               = var.project_id
  name                  = "cp-${replace(each.key, "_", "-")}"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = each.value
  }
  depends_on = [google_project_service.apis]
}

# What people use: IAP signs them in with Google and checks the tool's access list.
resource "google_compute_backend_service" "people" {
  for_each              = toset(local.people_backends)
  project               = var.project_id
  name                  = "cp-${each.key}"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  backend {
    group = google_compute_region_network_endpoint_group.neg[each.key].id
  }
  iap {
    enabled = true # Google-managed OAuth client: internal (nscale.com) accounts only
  }
  log_config {
    enable      = true
    sample_rate = 1
  }
}

# What devices use: no IAP (they can't do Google sign-in); the apps check the device keys, and
# Cloud Armor limits how fast any one address can call.
resource "google_compute_backend_service" "devices" {
  for_each              = toset(local.device_backends)
  project               = var.project_id
  name                  = "cp-${replace(each.key, "_", "-")}"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = google_compute_security_policy.devices.id
  backend {
    group = google_compute_region_network_endpoint_group.neg[each.key].id
  }
  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_iap_web_backend_service_iam_binding" "access" {
  for_each            = toset(local.people_backends)
  project             = var.project_id
  web_backend_service = google_compute_backend_service.people[each.key].name
  role                = "roles/iap.httpsResourceAccessor"
  members             = local.access[each.key]
}

resource "google_compute_security_policy" "devices" {
  project     = var.project_id
  name        = "cp-devices"
  description = "Device endpoints: rate limit per source address"

  rule {
    action   = "throttle"
    priority = 1000
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        # A whole office behind one NAT address checks in once a minute per device.
        count        = var.device_rate_limit_per_minute
        interval_sec = 60
      }
    }
    description = "Throttle any one address"
  }

  rule {
    action   = "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "Default allow"
  }
}

resource "google_compute_url_map" "https" {
  project         = var.project_id
  name            = "client-platforms"
  default_service = google_compute_backend_service.people["portal"].id

  host_rule {
    hosts        = [var.domain]
    path_matcher = "tools"
  }

  path_matcher {
    name            = "tools"
    default_service = google_compute_backend_service.people["portal"].id

    # Exact device endpoints first: these skip IAP. Longest match wins, so nothing else under
    # /notifications or /elevation can be reached without signing in.
    path_rule {
      paths   = ["/notifications/api/v1/checkin"]
      service = google_compute_backend_service.devices["notify_devices"].id
    }
    path_rule {
      paths   = ["/elevation/api/elevations/ingest"]
      service = google_compute_backend_service.devices["elevation_ingest"].id
    }
    path_rule {
      paths   = ["/notifications", "/notifications/*"]
      service = google_compute_backend_service.people["notify"].id
    }
    path_rule {
      paths   = ["/elevation", "/elevation/*"]
      service = google_compute_backend_service.people["elevation"].id
    }
  }
}

resource "google_compute_target_https_proxy" "https" {
  project          = var.project_id
  name             = "client-platforms"
  url_map          = google_compute_url_map.https.id
  ssl_certificates = [google_compute_managed_ssl_certificate.cert.id]
}

resource "google_compute_global_forwarding_rule" "https" {
  project               = var.project_id
  name                  = "client-platforms-https"
  target                = google_compute_target_https_proxy.https.id
  ip_address            = google_compute_global_address.lb.id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# http:// redirects to https://
resource "google_compute_url_map" "http_redirect" {
  project = var.project_id
  name    = "client-platforms-http"
  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "http" {
  project = var.project_id
  name    = "client-platforms-http"
  url_map = google_compute_url_map.http_redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  project               = var.project_id
  name                  = "client-platforms-http"
  target                = google_compute_target_http_proxy.http.id
  ip_address            = google_compute_global_address.lb.id
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# Optional: if nscale.com (or a zone for the subdomain) is in Cloud DNS in this project, set
# dns_managed_zone and the record is made here. Otherwise add the A record wherever nscale.com's DNS is.
resource "google_dns_record_set" "a" {
  count        = var.dns_managed_zone == "" ? 0 : 1
  project      = var.project_id
  managed_zone = var.dns_managed_zone
  name         = "${var.domain}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.lb.address]
}
