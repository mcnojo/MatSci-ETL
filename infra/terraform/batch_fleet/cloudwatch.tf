resource "aws_cloudwatch_log_group" "batch_worker" {
  name              = "/${var.name_prefix}/worker"
  retention_in_days = 14
}

# Target tracking on BacklogPerInstance = QueueDepth / MAX(InService, 1).
# The MAX guards against divide-by-zero when desired_capacity is 0; a
# non-empty queue still produces a finite signal that triggers scale-out.
# QueueDepth comes from prod/batch/queue_depth_publisher.py on cpu-pipeline-01.

resource "aws_autoscaling_policy" "cpu_backlog_per_instance" {
  name                   = "${var.name_prefix}-cpu-backlog-per-instance"
  autoscaling_group_name = aws_autoscaling_group.cpu.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value     = var.target_backlog_per_worker
    disable_scale_in = false

    customized_metric_specification {
      metrics {
        id          = "backlog"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "OCR/Batch"
            metric_name = "QueueDepth"
            dimensions {
              name  = "Queue"
              value = "cpu-task-queue"
            }
          }
        }
      }
      metrics {
        id          = "capacity"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "AWS/AutoScaling"
            metric_name = "GroupInServiceInstances"
            dimensions {
              name  = "AutoScalingGroupName"
              value = aws_autoscaling_group.cpu.name
            }
          }
        }
      }
      metrics {
        id          = "per_instance"
        expression  = "backlog / MAX([capacity, 1])"
        label       = "BacklogPerInstance (cpu)"
        return_data = true
      }
    }
  }
}

resource "aws_autoscaling_policy" "gpu_backlog_per_instance" {
  name                   = "${var.name_prefix}-gpu-backlog-per-instance"
  autoscaling_group_name = aws_autoscaling_group.gpu.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value     = var.target_backlog_per_worker
    disable_scale_in = false

    customized_metric_specification {
      metrics {
        id          = "backlog"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "OCR/Batch"
            metric_name = "QueueDepth"
            dimensions {
              name  = "Queue"
              value = "gpu-task-queue"
            }
          }
        }
      }
      metrics {
        id          = "capacity"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "AWS/AutoScaling"
            metric_name = "GroupInServiceInstances"
            dimensions {
              name  = "AutoScalingGroupName"
              value = aws_autoscaling_group.gpu.name
            }
          }
        }
      }
      metrics {
        id          = "per_instance"
        expression  = "backlog / MAX([capacity, 1])"
        label       = "BacklogPerInstance (gpu)"
        return_data = true
      }
    }
  }
}
