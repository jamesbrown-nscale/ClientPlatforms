# Nscale Client Platforms on Google Cloud.
#
#   https://clientplatforms.nscale.com  ──►  global HTTPS load balancer (Google-managed certificate)
#        │                                    Google sign-in for people: Identity-Aware Proxy (IAP)
#        ├─ /                                   portal        (IAP: portal_access)
#        ├─ /notifications/api/v1/checkin       notify        (devices: enrollment key, no IAP)
#        ├─ /notifications/*                    notify        (IAP: notifications_access)
#        ├─ /elevation/api/elevations/ingest    elevation     (devices: ingest key, no IAP)
#        └─ /elevation/*                        elevation     (IAP: elevation_access)
#
# Each tool is its own Cloud Run service (London) with its own service account and its own Firestore
# database, and can only read and write that database. Cloud Run only accepts traffic from the load
# balancer, so nothing can go round IAP; the apps also check IAP's signed header themselves.
#
# Terraform owns the infrastructure. Code is rolled out with ../deploy.sh, which builds an image and
# points the Cloud Run service at it (Terraform ignores the image).

locals {
  services = {
    portal = {
      service_name = "cp-portal"
      min          = 0
      max          = 2
      memory       = "256Mi"
    }
    notify = {
      service_name = "cp-notify"
      min          = 1 # devices check in every minute; one warm instance avoids cold starts
      max          = 3
      memory       = "512Mi"
    }
    elevation = {
      service_name = "cp-elevation"
      min          = 0
      max          = 2
      memory       = "512Mi"
    }
  }
  placeholder_image = "us-docker.pkg.dev/cloudrun/container/hello"
  project_number    = data.google_project.this.number
  # What each app checks the IAP header's audience against, so a sign-in meant for one tool can't be used on another.
  iap_audience = {
    for k, b in google_compute_backend_service.people :
    k => "/projects/${local.project_number}/global/backendServices/${b.generated_id}"
  }
}

data "google_project" "this" {
  project_id = var.project_id
}

# ---------- APIs ----------

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com", "compute.googleapis.com", "iap.googleapis.com", "firestore.googleapis.com",
    "secretmanager.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com",
    "iam.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------- Images ----------

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "client-platforms"
  format        = "DOCKER"
  description   = "Client Platforms service images"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 15
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days; the 15 most recent are kept above
    }
  }
  depends_on = [google_project_service.apis]
}

# Cloud Build runs as its own account that can only push images and write build logs.
resource "google_service_account" "build" {
  project      = var.project_id
  account_id   = "cp-build"
  display_name = "Client Platforms image builds"
}

resource "google_artifact_registry_repository_iam_member" "build_push" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/logging.logWriter", "roles/storage.objectViewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.build.email}"
}

# ---------- Data: one Firestore database per tool ----------

resource "google_firestore_database" "db" {
  for_each                = toset(["notifications", "elevation"])
  project                 = var.project_id
  name                    = each.value
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = var.deletion_protection ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  deletion_policy         = var.deletion_protection ? "ABANDON" : "DELETE"
  depends_on              = [google_project_service.apis]
}

resource "google_firestore_backup_schedule" "daily" {
  for_each  = google_firestore_database.db
  project   = var.project_id
  database  = each.value.name
  retention = "${var.backup_retention_days * 86400}s"
  daily_recurrence {}
}

# ---------- Service accounts: each app reaches only its own database and secrets ----------

resource "google_service_account" "app" {
  for_each     = local.services
  project      = var.project_id
  account_id   = each.value.service_name
  display_name = "Client Platforms ${each.key}"
}

resource "google_project_iam_member" "firestore" {
  for_each = { notify = "notifications", elevation = "elevation" }
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${google_service_account.app[each.key].email}"
  condition {
    title      = "only-${each.value}-database"
    expression = "resource.name == \"projects/${var.project_id}/databases/${each.value}\""
  }
}

# ---------- Secrets ----------
# Generated once here. Rotate with ../deploy.sh (which adds a new version); Terraform then ignores the value.

resource "random_password" "enrollment_key" {
  length  = 48
  special = false
}

resource "random_password" "ingest_key" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "s" {
  for_each  = toset(["cp-notify-enrollment-key", "cp-elevation-ingest-keys"])
  project   = var.project_id
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "enrollment_key" {
  secret      = google_secret_manager_secret.s["cp-notify-enrollment-key"].id
  secret_data = random_password.enrollment_key.result
  lifecycle {
    ignore_changes = [secret_data]
  }
}

# One "label:key" per line. Give each group of devices its own label and key so one can be revoked alone.
resource "google_secret_manager_secret_version" "ingest_keys" {
  secret      = google_secret_manager_secret.s["cp-elevation-ingest-keys"].id
  secret_data = "windows:${random_password.ingest_key.result}"
  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "read" {
  for_each = {
    "cp-notify-enrollment-key" = "notify"
    "cp-elevation-ingest-keys" = "elevation"
  }
  project   = var.project_id
  secret_id = google_secret_manager_secret.s[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app[each.value].email}"
}

# ---------- Cloud Run services ----------

resource "google_cloud_run_v2_service" "app" {
  for_each            = local.services
  project             = var.project_id
  name                = each.value.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" # only via the load balancer
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.app[each.key].email
    scaling {
      min_instance_count = each.value.min
      max_instance_count = each.value.max
    }
    containers {
      image = local.placeholder_image
      resources {
        limits = {
          cpu    = "1"
          memory = each.value.memory
        }
        cpu_idle = true
      }

      env {
        name  = "ALLOWED_DOMAIN"
        value = var.allowed_domain
      }
      env {
        name  = "IAP_AUDIENCE"
        value = local.iap_audience[each.key]
      }

      dynamic "env" {
        for_each = each.key == "notify" ? {
          AUTH_MODE          = "iap"
          BASE_PATH          = "/notifications"
          STORE              = "firestore"
          FIRESTORE_DATABASE = "notifications"
          ADMIN_EMAILS       = var.notify_admin_emails
          ALLOWED_LINK_HOSTS = var.notify_allowed_link_hosts
          PUBLIC_URL         = "https://${var.domain}"
          } : each.key == "elevation" ? {
          BASE_PATH          = "/elevation"
          STORE              = "firestore"
          FIRESTORE_DATABASE = "elevation"
          PORTAL_URL         = "/"
        } : {}
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = each.key == "notify" ? { ENROLLMENT_KEY = "cp-notify-enrollment-key" } : each.key == "elevation" ? { INGEST_KEYS = "cp-elevation-ingest-keys" } : {}
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # deploy.sh rolls out new images; gcloud also sets its own client labels.
    ignore_changes = [template[0].containers[0].image, client, client_version, template[0].labels, labels]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.enrollment_key,
    google_secret_manager_secret_version.ingest_keys,
    google_secret_manager_secret_iam_member.read,
  ]
}

# Anyone may call the services, but ingress only lets the load balancer through, and the load balancer
# puts IAP in front of everything except the two device endpoints.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = google_cloud_run_v2_service.app
  project  = var.project_id
  location = var.region
  name     = each.value.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
