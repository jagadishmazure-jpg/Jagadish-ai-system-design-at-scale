variable "prefix" {
  type    = string
  default = "kestrel"
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "node_count" {
  type    = number
  default = 3
}

variable "api_image" {
  type        = string
  description = "Image tagged with the git SHA by CI"
  default     = "ghcr.io/example/kestrel-api:sha"
}
