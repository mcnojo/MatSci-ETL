# Gateway endpoint for S3. Required so VPC-bound consumers (e.g. the batch
# trigger Lambda) can reach S3 without a NAT gateway. Free, scoped to the
# selected VPC, associates with ALL route tables in the VPC.
data "aws_route_tables" "all" {
  vpc_id = data.aws_vpc.selected.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = data.aws_vpc.selected.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.all.ids

  tags = {
    Name = "${var.name_prefix}-s3-gateway"
  }
}
