output "aks_name" {
  value = azurerm_kubernetes_cluster.aks.name
}

output "api_image" {
  value = var.api_image
}
