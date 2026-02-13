package om.log0.normalisation_service.consumer;

import lombok.extern.slf4j.Slf4j;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;

@Slf4j
@Component
public class RawLogConsumer {

    @KafkaListener(
            topics = "raw-logs",
            containerFactory = "kafkaListenerContainerFactory"
    )
    public void consume(String message, Acknowledgment ack) {
        try {
            log.info("Received raw log message: {}", message);

            // For now, we just acknowledge the message. The actual processing and normalisation will be implemented later.
            ack.acknowledge();
        } catch (Exception e) {
            log.error("Error processing raw log message: {}", e.getMessage(), e);
        }
    }
}
